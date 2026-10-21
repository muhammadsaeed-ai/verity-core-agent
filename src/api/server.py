"""FastAPI run/status API, SQLite task record, and n8n completion callback."""

from __future__ import annotations

import hmac
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.security import APIKeyHeader

from src.config import Settings
from src.graph.workflow import WorkflowRuntime, build_workflow, initial_state
from src.api.observability import TaskEventStore
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
    """Send a trusted callback without changing the workflow outcome on failure."""
    if not url:
        return
    try:
        parsed = urlparse(url)
        _ = parsed.port
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            logger.warning("N8N_WEBHOOK_URL is not a valid HTTP(S) URL; callback skipped.")
            return
    except ValueError:
        logger.warning("N8N_WEBHOOK_URL is malformed; callback skipped.")
        return
    request = Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Verity-1/1.0"}, method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            if response.status >= 400:
                logger.warning("n8n callback returned HTTP %s.", response.status)
    except Exception as exc:
        logger.warning("n8n callback failed without changing task status (%s).", type(exc).__name__)


def _append_event_safely(
    events: TaskEventStore, task_id: str, event_type: str, payload: dict[str, Any] | None = None
) -> None:
    """Audit events are best-effort; an observability outage must not fail the workflow."""
    try:
        events.append(task_id, event_type, payload)
    except Exception:
        logger.exception(
            "Could not persist audit event %s for task %s; execution will continue.",
            event_type, task_id,
        )


def create_app(settings: Settings | None = None, workflow: WorkflowRuntime | None = None) -> FastAPI:
    chosen = settings or Settings.from_env()
    runtime = workflow or build_workflow(chosen)
    store = TaskStore(chosen.database_path)
    events = TaskEventStore(chosen.database_path)
    task_slots = threading.BoundedSemaphore(chosen.max_active_tasks)
    app = FastAPI(
        title="Verity-1 Agent API", version="1.0.0",
        description="Hybrid autonomous workflow runner with Pydantic contracts and SQLite-backed state.",
    )
    app.state.verity_settings = chosen
    app.state.verity_workflow = runtime
    app.state.verity_store = store
    app.state.verity_events = events
    app.state.verity_task_slots = task_slots

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
        config = {"configurable": {"thread_id": task_id}}
        try:
            _append_event_safely(events, task_id, "task.started", {"objective_length": len(objective)})
            store.update(task_id, "running", state)
            _append_event_safely(events, task_id, "task.status_changed", {"status": "running"})
            result = runtime.invoke(state, config=config)
            status = str(result.get("status", "failed"))
            if status not in {"completed", "needs_review", "failed"}:
                raise RuntimeError("Workflow returned an invalid terminal status.")
        except Exception as exc:
            logger.exception("Task %s failed.", task_id)
            failed_state = dict(state)
            try:
                snapshot = runtime.get_state(config)
                checkpoint_values = getattr(snapshot, "values", {}) or {}
                if isinstance(checkpoint_values, dict):
                    failed_state.update(checkpoint_values)
            except Exception:
                logger.debug("Unable to load partial state after task failure.", exc_info=True)
            safe_error = f"{type(exc).__name__}: task execution failed. Inspect server logs using the task ID."
            failed_state.update(status="failed", error=safe_error)
            try:
                store.update(task_id, "failed", failed_state)
            except Exception:
                logger.exception("Could not persist failed status for task %s.", task_id)
            _append_event_safely(events, task_id, "task.failed", {"error_type": type(exc).__name__})
            _post_webhook(chosen.n8n_webhook_url, {
                "event": "verity.task.failed", "task_id": task_id,
                "status": "failed", "error": safe_error,
            })
            return

        try:
            store.update(task_id, status, result)
        except Exception:
            logger.exception("Workflow for task %s returned %s but task status persistence failed.", task_id, status)
            _append_event_safely(events, task_id, "task.status_persistence_failed", {"workflow_status": status})
            return

        _append_event_safely(
            events, task_id, "task.status_changed",
            {"status": status, "retry_count": result.get("retry_count", 0)},
        )
        if status == "failed":
            _append_event_safely(events, task_id, "task.failed", {"error_type": "WorkflowFailed"})
            callback_payload = {
                "event": "verity.task.failed", "task_id": task_id, "status": status,
                "error": "Workflow returned a failed terminal status.",
            }
        else:
            _append_event_safely(
                events, task_id, "task.finished",
                {"status": status, "artifact_count": len(result.get("execution_artifacts", []))},
            )
            callback_payload = {
                "event": "verity.task.completed" if status == "completed" else "verity.task.finished",
                "task_id": task_id, "status": status, "objective": objective,
                "retry_count": result.get("retry_count", 0), "qa_feedback": result.get("qa_feedback", {}),
                "artifacts": result.get("execution_artifacts", []), "report": result.get("final_report", {}),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        _post_webhook(chosen.n8n_webhook_url, callback_payload)

    def run_task_with_slot(task_id: str, objective: str) -> None:
        try:
            run_task(task_id, objective)
        finally:
            task_slots.release()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "verity-1-agent-api", "version": "1.0.0"}

    @app.post("/api/v1/agent/run", status_code=202)
    def run_agent(
        request: TaskRunRequest,
        background_tasks: BackgroundTasks,
        _auth: None = Depends(require_api_key),
    ) -> dict[str, Any]:
        if not task_slots.acquire(blocking=False):
            raise HTTPException(
                status_code=429,
                detail=f"Task capacity is full ({chosen.max_active_tasks} active or queued per process). Retry after a task finishes.",
                headers={"Retry-After": "5"},
            )
        state = initial_state(request.task_id, request.user_objective)
        try:
            store.create(request.task_id, request.user_objective, state)
        except sqlite3.IntegrityError as exc:
            task_slots.release()
            raise HTTPException(status_code=409, detail="task_id already exists; use a new task_id.") from exc
        except Exception:
            task_slots.release()
            raise
        _append_event_safely(events, request.task_id, "task.queued", {"objective_length": len(request.user_objective)})
        try:
            background_tasks.add_task(run_task_with_slot, request.task_id, request.user_objective)
        except Exception:
            task_slots.release()
            raise
        return {
            "task_id": request.task_id, "status": "queued",
            "status_url": f"/api/v1/agent/status/{request.task_id}",
            "message": "Task accepted. Poll the status URL for persisted workflow state.",
        }

    @app.get("/api/v1/agent/tasks")
    def list_agent_tasks(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        status: str | None = Query(default=None, pattern=r"^(queued|running|completed|needs_review|failed)$"),
        _auth: None = Depends(require_api_key),
    ) -> dict[str, Any]:
        items = events.list_tasks(limit=limit, offset=offset, status=status)
        return {"items": items, "limit": limit, "offset": offset, "count": len(items), "status_filter": status}

    @app.get("/api/v1/agent/status/{task_id}/events")
    def get_agent_events(
        task_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        after_id: int = Query(default=0, ge=0),
        _auth: None = Depends(require_api_key),
    ) -> dict[str, Any]:
        if not task_id or len(task_id) > 80 or any(not (ch.isalnum() or ch in "_-") for ch in task_id):
            raise HTTPException(status_code=422, detail="Invalid task_id.")
        if store.get(task_id) is None:
            raise HTTPException(status_code=404, detail="Task was not found.")
        items = events.list_events(task_id, limit=limit, after_id=after_id)
        return {"task_id": task_id, "items": items, "limit": limit, "after_id": after_id,
                "next_after_id": items[-1]["event_id"] if items else after_id}

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
        record_state = (record or {}).get("state", {})
        selected = {**record_state, **values}
        status = (record or {}).get("status") or selected.get("status", "unknown")
        return {
            "task_id": task_id, "status": status, "retry_count": selected.get("retry_count", 0),
            "qa_feedback": selected.get("qa_feedback", {}), "artifacts": selected.get("execution_artifacts", []),
            "report": selected.get("final_report", {}), "error": selected.get("error"),
            "created_at": (record or {}).get("created_at"), "updated_at": (record or {}).get("updated_at"),
        }

    return app


app = create_app()
