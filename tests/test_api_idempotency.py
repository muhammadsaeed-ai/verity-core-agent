from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from fastapi.testclient import TestClient

from src.api.server import TaskStore, create_app
from src.config import Settings
from src.graph.workflow import build_workflow, initial_state


API_KEY = "test-verification-key-0123456789"


def _settings(tmp_path):
    return replace(
        Settings.from_env(),
        dry_run_test_mode=True,
        research_enabled=False,
        enable_crewai=False,
        database_path=tmp_path / "idempotency.sqlite3",
        output_dir=tmp_path / "outputs",
        api_key=API_KEY,
        max_active_tasks=2,
    )


def test_idempotency_replay_returns_original_task_without_second_queue_event(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings=settings, workflow=build_workflow(settings))
    headers = {"X-API-Key": API_KEY, "Idempotency-Key": "idem-key-test-0001"}

    with TestClient(app) as client:
        first = client.post(
            "/api/v1/agent/run",
            headers=headers,
            json={
                "task_id": "idem-task-original",
                "objective": "Generate a tested task report and audit trail.",
            },
        )
        replay = client.post(
            "/api/v1/agent/run",
            headers=headers,
            json={
                "task_id": "idem-task-retry",
                "objective": "Generate a tested task report and audit trail.",
            },
        )

    assert first.status_code == 202
    assert replay.status_code == 202
    assert first.json()["task_id"] == replay.json()["task_id"] == "idem-task-original"
    assert replay.json()["idempotent_replay"] is True
    events = app.state.verity_events.list_events("idem-task-original", limit=100)
    assert sum(event["event_type"] == "task.queued" for event in events) == 1
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM verity_tasks").fetchone()[0] == 1


def test_idempotency_key_cannot_be_reused_for_a_different_objective(tmp_path):
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings=settings, workflow=build_workflow(settings)))
    headers = {"X-API-Key": API_KEY, "Idempotency-Key": "idem-key-test-0002"}

    first = client.post(
        "/api/v1/agent/run",
        headers=headers,
        json={"task_id": "idem-diff-1", "objective": "Create a sourced report for an agent task."},
    )
    conflict = client.post(
        "/api/v1/agent/run",
        headers=headers,
        json={"task_id": "idem-diff-2", "objective": "Create a security review for a different task."},
    )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert "different objective" in conflict.json()["detail"]


def test_idempotency_key_format_is_validated(tmp_path):
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings=settings, workflow=build_workflow(settings)))
    response = client.post(
        "/api/v1/agent/run",
        headers={"X-API-Key": API_KEY, "Idempotency-Key": "short"},
        json={"objective": "Validate request idempotency key rules."},
    )
    assert response.status_code == 422


def test_concurrent_same_key_creates_only_one_task(tmp_path):
    database = tmp_path / "concurrent.sqlite3"
    store = TaskStore(database)

    def submit(task_id: str):
        return store.create_idempotent(
            task_id,
            "Run the same objective once despite concurrent client retries.",
            initial_state(task_id, "Run the same objective once despite concurrent client retries."),
            "concurrent-key-0001",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ["concurrent-task-a", "concurrent-task-b"]))

    assert sorted(result[0] for result in results) == ["created", "replay"]
    assert results[0][1] == results[1][1]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM verity_tasks").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM verity_idempotency_keys").fetchone()[0] == 1


def test_concurrent_key_conflict_does_not_create_second_task(tmp_path):
    database = tmp_path / "concurrent-conflict.sqlite3"
    store = TaskStore(database)

    def submit(task_id: str, objective: str):
        return store.create_idempotent(
            task_id, objective, initial_state(task_id, objective), "concurrent-key-0002"
        )

    payloads = [
        ("conflict-task-a", "Create an auditable report for the first workflow."),
        ("conflict-task-b", "Build a different security assessment for this workflow."),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: submit(*item), payloads))

    assert sorted(result[0] for result in results) == ["conflict", "created"]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM verity_tasks").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM verity_idempotency_keys").fetchone()[0] == 1
