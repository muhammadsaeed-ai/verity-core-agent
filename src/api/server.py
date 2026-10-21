"""FastAPI run/status API, SQLite task record, and n8n completion callback."""

from __future__ import annotations

import hmac
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.security import APIKeyHeader

from src.config import Settings
from src.graph.workflow import WorkflowRuntime, build_workflow, initial_state
from src.schemas import TaskRunRequest

logger = logging.getLogger(__name__)


class TaskStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(database_path), timeout=30) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS verity_tasks (task_id TEXT PRIMARY KEY, objective TEXT NOT NULL, "
                "status TEXT NOT NULL, state_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )

    def create(self, task_id: str, objective: str, state: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(str(self.database_path), timeout=30) as connection:
            connection.execute(
                "INSERT INTO verity_tasks(task_id, objective, status, state_json, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                (task_id, objective, "queued", json.dumps(state, ensure_ascii=False), now, now),
            )

    def update(self, task_id: str, status: str, state: dict[str, Any]) -> None:
        with sqlite3.connect(str(self.database_path), timeout=30) as connection:
            connection.execute(
                "UPDATE verity_tasks SET status=?, state_json=?, updated_at=? WHERE task_id=?",
                (status, json.dumps(state, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), task_id),
            )

    def get(self, task_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(str(self.database_path), timeout=30) as connection:
            row = connection.execute(
                "SELECT task_id, objective, status, state_json, created_at, updated_at FROM verity_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "task_id": row[0], "objective": row[1], "status": row[2],
            "state": json.loads(row[3]), "created_at": row[4], "updated_at": row[5],
        }


def _post_webhook(url: str, payload: dict[str, Any]) -> None:
    if not url:
        return
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        logger.warning("N8N_WEBHOOK_URL is not a valid HTTP(S) URL; callback skipped.")
        return
    request = Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Verity-1/1.0"}, method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            if response.status >= 400:
                logger.warning("n8n callback returned HTTP %s.", response.status)
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning("n8n callback failed without changing task status: %s", exc)


def create_app(settings: Settings | None = None, workflow: WorkflowRuntime | None = None) -> FastAPI:
    chosen = settings or Settings.from_env()
    runtime = workflow or build_workflow(chosen)
    store = TaskStore(chosen.database_path)
    app = FastAPI(
        title="Verity-1 Agent API", version="1.0.0",
        description="Hybrid autonomous workflow runner with Pydantic contracts and SQLite-backed state.",
    )
    app.state.verity_settings = chosen
    app.state.verity_workflow = runtime
    app.state.verity_store = store

    api_key_header = APIKeyHeader(
        name="X-API-Key", scheme_name="Verity API Key", auto_error=False
    )

    def require_api_key(supplied_key: str | None = Depends(api_key_header)) -> None:
        """Fail closed: task APIs remain disabled until an operator configures a secret."""
        expected = chosen.api_key
        if len(expected) < 32:
            raise HTTPException(
                status_code=503,
                detail="Protected API endpoints are disabled. Configure VERITY_API_KEY with at least 32 characters.",
            )
        supplied = supplied_key or ""
        if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(
                status_code=401,
                detail="Missing or invalid API key.",
                headers={"WWW-Authenticate": "ApiKey"},
            )

    def run_task(task_id: str, objective: str) -> None:
        state = initial_state(task_id, objective)
        store.update(task_id, "running", state)
        try:
            result = runtime.invoke(state, config={"configurable": {"thread_id": task_id}})
            status = str(result.get("status", "failed"))
            store.update(task_id, status, result)
            _post_webhook(chosen.n8n_webhook_url, {
                "event": "verity.task.completed" if status == "completed" else "verity.task.finished",
                "task_id": task_id, "status": status, "objective": objective,
                "retry_count": result.get("retry_count", 0), "qa_feedback": result.get("qa_feedback", {}),
                "artifacts": result.get("execution_artifacts", []), "report": result.get("final_report", {}),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as exc:
            logger.exception("Task %s failed.", task_id)
            failed = {**state, "status": "failed", "error": type(exc).__name__ + ": " + str(exc)[:800]}
            store.update(task_id, "failed", failed)
            _post_webhook(chosen.n8n_webhook_url, {
                "event": "verity.task.failed", "task_id": task_id,
                "status": "failed", "error": failed["error"],
            })

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "verity-1-agent-api", "version": "1.0.0"}

    @app.post("/api/v1/agent/run", status_code=202)
    def run_agent(
        request: TaskRunRequest,
        background_tasks: BackgroundTasks,
        _auth: None = Depends(require_api_key),
    ) -> dict[str, Any]:
        state = initial_state(request.task_id, request.user_objective)
        try:
            store.create(request.task_id, request.user_objective, state)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="task_id already exists; use a new task_id.") from exc
        background_tasks.add_task(run_task, request.task_id, request.user_objective)
        return {
            "task_id": request.task_id, "status": "queued",
            "status_url": f"/api/v1/agent/status/{request.task_id}",
            "message": "Task accepted. Poll the status URL for persisted workflow state.",
        }

    @app.get("/api/v1/agent/status/{task_id}")
    def get_agent_status(task_id: str, _auth: None = Depends(require_api_key)) -> dict[str, Any]:
        if not task_id or len(task_id) > 80 or any(not (ch.isalnum() or ch in "_-") for ch in task_id):
            raise HTTPException(status_code=422, detail="Invalid task_id.")
        record = store.get(task_id)
        try:
            snapshot = runtime.get_state({"configurable": {"thread_id": task_id}})
            values = getattr(snapshot, "values", {}) or {}
        except Exception as exc:
            logger.warning("Checkpoint lookup failed for %s: %s", task_id, exc)
            values = {}
        if record is None and not values:
            raise HTTPException(status_code=404, detail="Task was not found.")
        selected = values or (record or {}).get("state", {})
        status = (record or {}).get("status") or selected.get("status", "unknown")
        return {
            "task_id": task_id, "status": status, "retry_count": selected.get("retry_count", 0),
            "qa_feedback": selected.get("qa_feedback", {}), "artifacts": selected.get("execution_artifacts", []),
            "report": selected.get("final_report", {}), "error": selected.get("error"),
            "created_at": (record or {}).get("created_at"), "updated_at": (record or {}).get("updated_at"),
        }

    return app


app = create_app()
