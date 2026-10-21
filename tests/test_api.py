from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from src.api.server import create_app
from src.config import Settings
from src.graph.workflow import build_workflow


def test_api_accepts_run_and_returns_persisted_status(tmp_path) -> None:
    settings = replace(
        Settings.from_env(), dry_run_test_mode=True, research_enabled=False, enable_crewai=False,
        database_path=tmp_path / "api.sqlite3", output_dir=tmp_path / "outputs", n8n_webhook_url="",
    )
    runtime = build_workflow(settings)
    client = TestClient(create_app(settings=settings, workflow=runtime))
    response = client.post(
        "/api/v1/agent/run",
        json={"task_id": "api-test-01", "objective": "Validate the API workflow and ensure the final report is persisted."},
    )
    assert response.status_code == 202
    assert response.json()["task_id"] == "api-test-01"
    status = client.get("/api/v1/agent/status/api-test-01")
    assert status.status_code == 200
    body = status.json()
    assert body["status"] == "completed"
    assert body["report"]["task_id"] == "api-test-01"
    assert client.get("/health").json()["status"] == "ok"


def test_api_returns_404_for_unknown_task(tmp_path) -> None:
    settings = replace(
        Settings.from_env(), dry_run_test_mode=True,
        database_path=tmp_path / "api-missing.sqlite3", output_dir=tmp_path / "outputs",
    )
    runtime = build_workflow(settings)
    client = TestClient(create_app(settings=settings, workflow=runtime))
    assert client.get("/api/v1/agent/status/not-found").status_code == 404
