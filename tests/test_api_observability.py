from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from src.api.server import create_app
from src.config import Settings
from src.graph.workflow import build_workflow


def test_task_inventory_and_audit_endpoints_are_authenticated_and_operational(tmp_path):
    settings = replace(
        Settings.from_env(),
        dry_run_test_mode=True,
        research_enabled=False,
        enable_crewai=False,
        database_path=tmp_path / "api-observability.sqlite3",
        output_dir=tmp_path / "outputs",
        api_key="test-verification-key-0123456789",
    )
    client = TestClient(create_app(settings=settings, workflow=build_workflow(settings)))
    headers = {"X-API-Key": "test-verification-key-0123456789"}

    queued = client.post(
        "/api/v1/agent/run",
        json={
            "task_id": "audit-api-task-01",
            "objective": "Verify task inventory and chronological audit endpoints.",
        },
        headers=headers,
    )
    assert queued.status_code == 202

    inventory = client.get("/api/v1/agent/tasks?limit=10&status=completed", headers=headers)
    assert inventory.status_code == 200
    assert any(item["task_id"] == "audit-api-task-01" for item in inventory.json()["items"])

    events = client.get("/api/v1/agent/status/audit-api-task-01/events?limit=100", headers=headers)
    assert events.status_code == 200
    body = events.json()
    types = [item["event_type"] for item in body["items"]]
    assert "task.queued" in types
    assert "task.started" in types
    assert "task.finished" in types
    assert types.index("task.queued") < types.index("task.started") < types.index("task.finished")

    cursor = body["next_after_id"]
    incremental = client.get(
        f"/api/v1/agent/status/audit-api-task-01/events?after_id={cursor}",
        headers=headers,
    )
    assert incremental.status_code == 200
    assert incremental.json()["items"] == []

    assert client.get("/api/v1/agent/tasks").status_code == 401
    assert client.get("/api/v1/agent/status/audit-api-task-01/events").status_code == 401


def test_audit_endpoint_returns_404_for_unknown_task(tmp_path):
    settings = replace(
        Settings.from_env(),
        dry_run_test_mode=True,
        research_enabled=False,
        enable_crewai=False,
        database_path=tmp_path / "api-observability-missing.sqlite3",
        output_dir=tmp_path / "outputs",
        api_key="test-verification-key-0123456789",
    )
    client = TestClient(create_app(settings=settings, workflow=build_workflow(settings)))
    response = client.get(
        "/api/v1/agent/status/not-created/events",
        headers={"X-API-Key": "test-verification-key-0123456789"},
    )
    assert response.status_code == 404
