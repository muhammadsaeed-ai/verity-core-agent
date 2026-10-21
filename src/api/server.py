"""FastAPI run/status API, SQLite task record, and n8n completion callback."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.security import APIKeyHeader

from src.config import Settings
from src.graph.workflow import WorkflowRuntime, build_workflow, initial_state
from src.api.observability import TaskEventStore
from src.api.integrity import verify_artifact_records
from src.schemas import TaskRunRequest

logger = logging.getLogger(__name__)


class TaskQueueFullError(RuntimeError):
    """Raised when the durable queue reaches its configured admission limit."""


class TaskStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(database_path), timeout=30) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS verity_tasks (task_id TEXT PRIMARY KEY, objective TEXT NOT NULL, "
                "status TEXT NOT NULL, state_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "lease_owner TEXT, lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0)"
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(verity_tasks)")}
            if "lease_owner" not in columns:
                connection.execute("ALTER TABLE verity_tasks ADD COLUMN lease_owner TEXT")
            if "lease_until" not in columns:
                connection.execute("ALTER TABLE verity_tasks ADD COLUMN lease_until TEXT")
            if "attempts" not in columns:
                connection.execute("ALTER TABLE verity_tasks ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS verity_idempotency_keys ("
                "idempotency_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE, "
                "created_at TEXT NOT NULL)"
            )

    @staticmethod
    def _pending_count(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COUNT(*) FROM verity_tasks WHERE status IN ('queued', 'running')"
        ).fetchone()
        return int(row[0] if row else 0)

    def count_pending(self) -> int:
        with sqlite3.connect(str(self.database_path), timeout=30) as connection:
            return self._pending_count(connection)

    def create(
        self,
        task_id: str,
        objective: str,
        state: dict[str, Any],
        max_pending: int = 100,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(str(self.database_path), timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if self._pending_count(connection) >= max_pending:
                    raise TaskQueueFullError("The durable task queue is full; retry after queued work drains.")
                connection.execute(
                    "INSERT INTO verity_tasks(task_id, objective, status, state_json, created_at, updated_at, "
                    "lease_owner, lease_until, attempts) VALUES(?, ?, ?, ?, ?, ?, NULL, NULL, 0)",
                    (task_id, objective, "queued", json.dumps(state, ensure_ascii=False), now, now),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def request_fingerprint(objective: str) -> str:
        """Return a stable digest without storing another copy of the objective."""
        return hashlib.sha256(objective.strip().encode("utf-8")).hexdigest()

    def lookup_idempotency(self, idempotency_key: str) -> dict[str, str] | None:
        with sqlite3.connect(str(self.database_path), timeout=30) as connection:
            row = connection.execute(
                "SELECT request_hash, task_id FROM verity_idempotency_keys WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return {"request_hash": row[0], "task_id": row[1]}

    def create_idempotent(
        self, task_id: str, objective: str, state: dict[str, Any], idempotency_key: str,
        max_pending: int = 100,
    ) -> tuple[str, str]:
        """Atomically create a task and bind its idempotency key.

        Returns (created|replay|conflict, task_id). BEGIN IMMEDIATE serializes
        concurrent first submissions so a single key cannot launch two tasks.
        """
        now = datetime.now(timezone.utc).isoformat()
        request_hash = self.request_fingerprint(objective)
        with sqlite3.connect(str(self.database_path), timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT request_hash, task_id FROM verity_idempotency_keys WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    if existing[0] != request_hash:
                        return "conflict", existing[1]
                    return "replay", existing[1]
                if self._pending_count(connection) >= max_pending:
                    raise TaskQueueFullError("The durable task queue is full; retry after queued work drains.")
                connection.execute(
                    "INSERT INTO verity_tasks(task_id, objective, status, state_json, created_at, updated_at, "
                    "lease_owner, lease_until, attempts) VALUES(?, ?, ?, ?, ?, ?, NULL, NULL, 0)",
                    (task_id, objective, "queued", json.dumps(state, ensure_ascii=False), now, now),
                )
                connection.execute(
                    "INSERT INTO verity_idempotency_keys(idempotency_key, request_hash, task_id, created_at) "
                    "VALUES(?, ?, ?, ?)",
                    (idempotency_key, request_hash, task_id, now),
                )
                connection.commit()
                return "created", task_id
            except Exception:
                connection.rollback()
                raise

    def update(self, task_id: str, status: str, state: dict[str, Any]) -> None:
        with sqlite3.connect(str(self.database_path), timeout=30) as connection:
            now = datetime.now(timezone.utc).isoformat()
            if status in {"completed", "needs_review", "failed"}:
                connection.execute(
                    "UPDATE verity_tasks SET status=?, state_json=?, updated_at=?, lease_owner=NULL, lease_until=NULL "
                    "WHERE task_id=?",
                    (status, json.dumps(state, ensure_ascii=False), now, task_id),
                )
            elif status == "queued":
                connection.execute(
                    "UPDATE verity_tasks SET status=?, state_json=?, updated_at=?, lease_owner=NULL, lease_until=NULL "
                    "WHERE task_id=?",
                    (status, json.dumps(state, ensure_ascii=False), now, task_id),
                )
            else:
                connection.execute(
                    "UPDATE verity_tasks SET status=?, state_json=?, updated_at=? WHERE task_id=?",
                    (status, json.dumps(state, ensure_ascii=False), now, task_id),
                )

    def claim_next(self, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        """Atomically claim one queued or stale running task, recovering after process death."""
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease_until = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        query = (
            "SELECT task_id, objective, state_json, attempts, status, lease_until FROM verity_tasks "
            "WHERE status='queued' OR (status='running' AND (lease_until IS NULL OR lease_until<=?)) "
            "ORDER BY created_at, task_id LIMIT 1"
        )
        with sqlite3.connect(str(self.database_path), timeout=30) as connection:
            # The steady-state empty poll must be read-only. BEGIN IMMEDIATE on every
            # 250ms poll starves LangGraph's SQLite checkpoint writes under load.
            if connection.execute(query, (now,)).fetchone() is None:
                return None
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Re-read after obtaining the write lock: another worker may have claimed
                # the candidate after the preliminary, lock-free read.
                row = connection.execute(query, (now,)).fetchone()
                if row is None:
                    connection.commit()
                    return None
                task_id = row[0]
                updated = connection.execute(
                    "UPDATE verity_tasks SET status='running', lease_owner=?, lease_until=?, attempts=attempts+1, updated_at=? "
                    "WHERE task_id=? AND (status='queued' OR (status='running' AND (lease_until IS NULL OR lease_until<=?)))",
                    (worker_id, lease_until, now, task_id, now),
                )
                if updated.rowcount != 1:
                    connection.rollback()
                    return None
                connection.commit()
                return {
                    "task_id": task_id,
                    "objective": row[1],
                    "state": json.loads(row[2]),
                    "attempts": int(row[3] or 0) + 1,
                    "previous_status": row[4],
                    "previous_lease_until": row[5],
                    "lease_owner": worker_id,
                    "lease_until": lease_until,
                }
            except Exception:
                connection.rollback()
                raise

    def heartbeat(self, task_id: str, worker_id: str, lease_seconds: int) -> bool:
        now_dt = datetime.now(timezone.utc)
        with sqlite3.connect(str(self.database_path), timeout=30) as connection:
            cursor = connection.execute(
                "UPDATE verity_tasks SET lease_until=?, updated_at=? "
                "WHERE task_id=? AND status='running' AND lease_owner=?",
                ((now_dt + timedelta(seconds=lease_seconds)).isoformat(), now_dt.isoformat(), task_id, worker_id),
            )
            return cursor.rowcount == 1

    def get(self, task_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(str(self.database_path), timeout=30) as connection:
            row = connection.execute(
                "SELECT task_id, objective, status, state_json, created_at, updated_at, attempts, lease_owner, lease_until "
                "FROM verity_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "task_id": row[0], "objective": row[1], "status": row[2],
            "state": json.loads(row[3]), "created_at": row[4], "updated_at": row[5],
            "attempts": int(row[6] or 0), "lease_owner": row[7], "lease_until": row[8],
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
    queue_capacity = max(chosen.max_active_tasks, chosen.max_active_tasks * 25)
    lease_seconds = max(30, min(300, chosen.model_timeout_seconds))
    worker_stop = threading.Event()
    worker_lock = threading.Lock()
    claim_lock = threading.Lock()
    worker_threads: list[threading.Thread] = []
    app = FastAPI(
        title="Verity-1 Agent API", version="1.0.0",
        description="Hybrid autonomous workflow runner with Pydantic contracts and SQLite-backed state.",
    )
    app.state.verity_settings = chosen
    app.state.verity_workflow = runtime
    app.state.verity_store = store
    app.state.verity_events = events
    app.state.verity_queue_capacity = queue_capacity
    app.state.verity_worker_threads = worker_threads
    app.state.verity_worker_stop = worker_stop

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

    def load_task_snapshot(task_id: str, record: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Use task rows for normal polling; read the graph only after a recorded persistence failure."""
        selected = dict(record.get("state", {}))
        status = str(record.get("status") or selected.get("status", "unknown"))
        if status in {"queued", "running"}:
            try:
                audit = events.list_events(task_id, limit=500)
                recovery = next((
                    event for event in reversed(audit)
                    if event.get("event_type") == "task.status_persistence_failed"
                    and event.get("payload", {}).get("workflow_status") in {"completed", "needs_review", "failed"}
                ), None)
            except Exception:
                logger.warning("Could not inspect recovery events for task %s.", task_id)
                recovery = None
            if recovery is not None:
                try:
                    snapshot = runtime.get_state({"configurable": {"thread_id": task_id}})
                    values = getattr(snapshot, "values", {}) or {}
                    if isinstance(values, dict):
                        selected.update(values)
                    status = recovery["payload"]["workflow_status"]
                except Exception:
                    logger.warning("Checkpoint recovery failed for task %s.", task_id)
        return selected, status

    def run_task(
        task_id: str,
        objective: str,
        recovered_state: dict[str, Any] | None = None,
    ) -> None:
        state = dict(recovered_state) if isinstance(recovered_state, dict) else initial_state(task_id, objective)
        state["task_id"] = task_id
        state["user_objective"] = objective
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

    def heartbeat_lease(task_id: str, worker_id: str, heartbeat_stop: threading.Event) -> None:
        """Keep an active claim leased across model/browser calls and transiently slow stages."""
        interval = max(5.0, lease_seconds / 3.0)
        while not heartbeat_stop.wait(interval):
            try:
                if not store.heartbeat(task_id, worker_id, lease_seconds):
                    logger.warning("Worker %s no longer owns the lease for task %s.", worker_id, task_id)
                    return
            except Exception:
                # Leave recovery to lease expiry if the database is temporarily unavailable.
                logger.exception("Lease heartbeat failed for task %s.", task_id)

    def queue_worker(worker_id: str) -> None:
        """Consume persistent tasks and retry abandoned running records after their lease expires."""
        current_thread = threading.current_thread()
        idle_since: float | None = None
        try:
            while not worker_stop.is_set():
                try:
                    # Serialize claims inside this process; SQLite leases still coordinate
                    # distinct processes, while this avoids a startup burst of writer locks.
                    with claim_lock:
                        claimed = store.claim_next(worker_id, lease_seconds)
                except Exception:
                    logger.exception("Worker %s could not claim a durable task.", worker_id)
                    worker_stop.wait(1.0)
                    continue

                if claimed is None:
                    if idle_since is None:
                        idle_since = time.monotonic()
                    if time.monotonic() - idle_since >= 2.0:
                        # Exit empty workers, but keep pending leased tasks under observation.
                        # This also prevents idle daemon-thread accumulation in short-lived clients.
                        with worker_lock:
                            try:
                                pending = store.count_pending()
                            except Exception:
                                logger.exception("Worker %s could not inspect queue depth.", worker_id)
                                pending = 1
                            if pending == 0:
                                worker_threads[:] = [
                                    thread for thread in worker_threads
                                    if thread is not current_thread and thread.is_alive()
                                ]
                                return
                        idle_since = time.monotonic()
                    worker_stop.wait(0.25)
                    continue

                idle_since = None
                task_id = claimed["task_id"]
                if claimed["attempts"] > 1:
                    _append_event_safely(
                        events,
                        task_id,
                        "task.recovered",
                        {"attempt": claimed["attempts"], "worker_id": worker_id},
                    )
                heartbeat_stop = threading.Event()
                heartbeat_thread = threading.Thread(
                    target=heartbeat_lease,
                    args=(task_id, worker_id, heartbeat_stop),
                    name=f"verity-heartbeat-{task_id[:24]}",
                    daemon=True,
                )
                heartbeat_thread.start()
                try:
                    run_task(task_id, claimed["objective"], claimed.get("state"))
                except Exception:
                    # run_task normally translates failures to a terminal state. If its own
                    # bookkeeping fails, let the lease expire so another worker can recover it.
                    logger.exception("Worker %s aborted task %s; its lease will expire.", worker_id, task_id)
                finally:
                    heartbeat_stop.set()
                    heartbeat_thread.join(timeout=1.0)
        finally:
            with worker_lock:
                worker_threads[:] = [
                    thread for thread in worker_threads
                    if thread is not current_thread and thread.is_alive()
                ]

    def start_workers() -> None:
        """Ensure the configured local worker pool is running (safe for API/lifespan calls)."""
        if worker_stop.is_set():
            return
        with worker_lock:
            worker_threads[:] = [thread for thread in worker_threads if thread.is_alive()]
            while len(worker_threads) < chosen.max_active_tasks:
                index = len(worker_threads) + 1
                worker_id = f"{uuid.uuid4().hex}:{index}"
                thread = threading.Thread(
                    target=queue_worker,
                    args=(worker_id,),
                    name=f"verity-task-worker-{index}",
                    daemon=True,
                )
                worker_threads.append(thread)
                thread.start()

    def stop_workers() -> None:
        worker_stop.set()
        current = threading.current_thread()
        with worker_lock:
            threads = list(worker_threads)
        for thread in threads:
            if thread is not current:
                thread.join(timeout=5.0)

    # On a real server startup, workers scan for queued work and expired leases left by a crash.
    # The API also calls start_workers after accepting work to support application runners which
    # don't invoke ASGI lifespan hooks (for example, some embedded/test clients).
    app.add_event_handler("startup", start_workers)
    app.add_event_handler("shutdown", stop_workers)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "verity-1-agent-api", "version": "1.0.0"}

    @app.post("/api/v1/agent/run", status_code=202)
    def run_agent(
        request: TaskRunRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        _auth: None = Depends(require_api_key),
    ) -> dict[str, Any]:
        if idempotency_key is not None:
            idempotency_key = idempotency_key.strip()
            if re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", idempotency_key) is None:
                raise HTTPException(
                    status_code=422,
                    detail="Idempotency-Key must be 8-128 characters using letters, numbers, dot, underscore, colon, or hyphen.",
                )
            prior = store.lookup_idempotency(idempotency_key)
            if prior is not None:
                if prior["request_hash"] != store.request_fingerprint(request.user_objective):
                    raise HTTPException(
                        status_code=409,
                        detail="Idempotency-Key was already used for a different objective.",
                    )
                existing_task = store.get(prior["task_id"])
                if existing_task is None:
                    logger.error("Idempotency record refers to missing task %s.", prior["task_id"])
                    raise HTTPException(status_code=500, detail="Idempotency record is inconsistent; contact the operator.")
                _, existing_status = load_task_snapshot(prior["task_id"], existing_task)
                return {
                    "task_id": prior["task_id"],
                    "status": existing_status,
                    "status_url": f"/api/v1/agent/status/{prior['task_id']}",
                    "message": "Returning the original task; this Idempotency-Key has already been processed.",
                    "idempotent_replay": True,
                }

        state = initial_state(request.task_id, request.user_objective)
        try:
            if idempotency_key is None:
                store.create(
                    request.task_id,
                    request.user_objective,
                    state,
                    max_pending=queue_capacity,
                )
                creation_status, actual_task_id = "created", request.task_id
            else:
                creation_status, actual_task_id = store.create_idempotent(
                    request.task_id,
                    request.user_objective,
                    state,
                    idempotency_key,
                    max_pending=queue_capacity,
                )
            if creation_status == "conflict":
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key was concurrently used for a different objective.",
                )
            if creation_status == "replay":
                existing_task = store.get(actual_task_id)
                if existing_task is None:
                    raise HTTPException(status_code=500, detail="Idempotency record is inconsistent; contact the operator.")
                _, existing_status = load_task_snapshot(actual_task_id, existing_task)
                return {
                    "task_id": actual_task_id,
                    "status": existing_status,
                    "status_url": f"/api/v1/agent/status/{actual_task_id}",
                    "message": "Returning the original task; this Idempotency-Key has already been processed.",
                    "idempotent_replay": True,
                }
        except TaskQueueFullError as exc:
            raise HTTPException(
                status_code=429,
                detail=str(exc),
                headers={"Retry-After": "5"},
            ) from exc
        except HTTPException:
            raise
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="task_id already exists; use a new task_id.") from exc

        _append_event_safely(events, request.task_id, "task.queued", {"objective_length": len(request.user_objective)})
        start_workers()
        return {
            "task_id": request.task_id, "status": "queued",
            "status_url": f"/api/v1/agent/status/{request.task_id}",
            "message": "Task accepted into the durable queue. Poll the status URL for persisted workflow state.",
        }

    @app.get("/api/v1/agent/tasks")
    def list_agent_tasks(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        status: str | None = Query(default=None, pattern=r"^(queued|running|completed|needs_review|failed)$"),
        _auth: None = Depends(require_api_key),
    ) -> dict[str, Any]:
        items = events.list_tasks(limit=limit, offset=offset, status=status)
        return {
            "items": items, "limit": limit, "offset": offset, "count": len(items),
            "total_count": events.count_tasks(status=status), "status_filter": status,
        }

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

    @app.get("/api/v1/agent/status/{task_id}/artifacts/verify")
    def verify_agent_artifacts(task_id: str, _auth: None = Depends(require_api_key)) -> dict[str, Any]:
        if not task_id or len(task_id) > 80 or any(not (ch.isalnum() or ch in "_-") for ch in task_id):
            raise HTTPException(status_code=422, detail="Invalid task_id.")
        record = store.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Task was not found.")
        selected, status = load_task_snapshot(task_id, record)
        result = verify_artifact_records(chosen.output_dir, task_id, selected.get("execution_artifacts", []))
        result["task_status"] = status
        return result
    @app.get("/api/v1/agent/status/{task_id}")
    def get_agent_status(task_id: str, _auth: None = Depends(require_api_key)) -> dict[str, Any]:
        if not task_id or len(task_id) > 80 or any(not (ch.isalnum() or ch in "_-") for ch in task_id):
            raise HTTPException(status_code=422, detail="Invalid task_id.")
        record = store.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Task was not found.")
        selected, status = load_task_snapshot(task_id, record)
        return {
            "task_id": task_id, "status": status, "retry_count": selected.get("retry_count", 0),
            "qa_feedback": selected.get("qa_feedback", {}), "artifacts": selected.get("execution_artifacts", []),
            "report": selected.get("final_report", {}), "error": selected.get("error"),
            "created_at": (record or {}).get("created_at"), "updated_at": (record or {}).get("updated_at"),
            "attempts": (record or {}).get("attempts", 0),
        }

    return app


app = create_app()
