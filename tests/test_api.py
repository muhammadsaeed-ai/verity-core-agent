from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.api.server import create_app
from src.config import Settings
from src.graph.workflow import build_workflow


def test_api_accepts_run_and_returns_persisted_status(tmp_path) -> None:
    settings = replace(
        Settings.from_env(), dry_run_test_mode=True, research_enabled=False, enable_crewai=False,
        database_path=tmp_path / "api.sqlite3", output_dir=tmp_path / "outputs", n8n_webhook_url="",
        api_key="test-verification-key-0123456789",
    )
    runtime = build_workflow(settings)
    client = TestClient(create_app(settings=settings, workflow=runtime))
    response = client.post(
        "/api/v1/agent/run",
        json={"task_id": "api-test-01", "objective": "Validate the API workflow and ensure the final report is persisted."},
        headers={"X-API-Key": "test-verification-key-0123456789"},
    )
    assert response.status_code == 202
    assert response.json()["task_id"] == "api-test-01"
    status = client.get("/api/v1/agent/status/api-test-01", headers={"X-API-Key": "test-verification-key-0123456789"})
    assert status.status_code == 200
    body = status.json()
    assert body["status"] == "completed"
    assert body["report"]["task_id"] == "api-test-01"
    assert client.get("/health").json()["status"] == "ok"


def test_api_returns_404_for_unknown_task(tmp_path) -> None:
    settings = replace(
        Settings.from_env(), dry_run_test_mode=True,
        database_path=tmp_path / "api-missing.sqlite3", output_dir=tmp_path / "outputs",
        api_key="test-verification-key-0123456789",
    )
    runtime = build_workflow(settings)
    client = TestClient(create_app(settings=settings, workflow=runtime))
    assert client.get(
        "/api/v1/agent/status/not-found", headers={"X-API-Key": "test-verification-key-0123456789"}
    ).status_code == 404



def test_protected_api_rejects_missing_and_wrong_keys(tmp_path) -> None:
    settings = replace(
        Settings.from_env(), dry_run_test_mode=True, research_enabled=False, enable_crewai=False,
        database_path=tmp_path / "api-auth.sqlite3", output_dir=tmp_path / "outputs",
        api_key="expected-secret-0123456789abcdef",
    )
    client = TestClient(create_app(settings=settings, workflow=build_workflow(settings)))
    payload = {"objective": "Test that the authenticated task API rejects bad credentials."}
    assert client.post("/api/v1/agent/run", json=payload).status_code == 401
    assert client.post(
        "/api/v1/agent/run", json=payload, headers={"X-API-Key": "wrong-secret"}
    ).status_code == 401
    assert client.get("/api/v1/agent/status/guessable-id").status_code == 401
    assert client.get("/health").status_code == 200


def test_protected_api_fails_closed_without_configured_key(tmp_path) -> None:
    settings = replace(
        Settings.from_env(), dry_run_test_mode=True, research_enabled=False, enable_crewai=False,
        database_path=tmp_path / "api-no-key.sqlite3", output_dir=tmp_path / "outputs",
        api_key="",
    )
    client = TestClient(create_app(settings=settings, workflow=build_workflow(settings)))
    response = client.post(
        "/api/v1/agent/run",
        json={"objective": "Check fail-closed API configuration behavior."},
    )
    assert response.status_code == 503
    assert "VERITY_API_KEY" in response.json()["detail"]



def test_openapi_documents_api_key_security_scheme(tmp_path) -> None:
    settings = replace(
        Settings.from_env(), dry_run_test_mode=True, research_enabled=False, enable_crewai=False,
        database_path=tmp_path / "api-openapi.sqlite3", output_dir=tmp_path / "outputs",
        api_key="expected-secret-0123456789abcdef",
    )
    client = TestClient(create_app(settings=settings, workflow=build_workflow(settings)))
    spec = client.get("/openapi.json").json()
    assert spec["components"]["securitySchemes"]["Verity API Key"]["name"] == "X-API-Key"
    assert spec["paths"]["/api/v1/agent/run"]["post"]["security"] == [{"Verity API Key": []}]



def test_malformed_webhook_url_does_not_change_success(tmp_path):
    settings = replace(
        Settings.from_env(), dry_run_test_mode=True, research_enabled=False, enable_crewai=False,
        database_path=tmp_path / "api-webhook.sqlite3", output_dir=tmp_path / "outputs",
        api_key="test-verification-key-0123456789", n8n_webhook_url="http://example.com:invalid/path",
    )
    client = TestClient(create_app(settings=settings, workflow=build_workflow(settings)))
    headers = {"X-API-Key": settings.api_key}
    response = client.post(
        "/api/v1/agent/run",
        json={"task_id": "webhook-safe-01", "objective": "Verify callback parsing cannot corrupt workflow status."},
        headers=headers,
    )
    assert response.status_code == 202
    status = client.get("/api/v1/agent/status/webhook-safe-01", headers=headers)
    assert status.status_code == 200
    assert status.json()["status"] == "completed"


def test_audit_store_failure_does_not_break_task(tmp_path, monkeypatch):
    settings = replace(
        Settings.from_env(), dry_run_test_mode=True, research_enabled=False, enable_crewai=False,
        database_path=tmp_path / "api-audit-outage.sqlite3", output_dir=tmp_path / "outputs",
        api_key="test-verification-key-0123456789",
    )
    app = create_app(settings=settings, workflow=build_workflow(settings))

    def broken_append(*args, **kwargs):
        raise OSError("simulated event store problem")

    monkeypatch.setattr(app.state.verity_events, "append", broken_append)
    client = TestClient(app)
    headers = {"X-API-Key": settings.api_key}
    response = client.post(
        "/api/v1/agent/run",
        json={"task_id": "audit-outage-01", "objective": "Verify audit write errors do not fail normal task execution."},
        headers=headers,
    )
    assert response.status_code == 202
    status = client.get("/api/v1/agent/status/audit-outage-01", headers=headers)
    assert status.status_code == 200
    assert status.json()["status"] == "completed"


def test_task_admission_returns_429_at_capacity(tmp_path):
    settings = replace(
        Settings.from_env(), dry_run_test_mode=True, research_enabled=False, enable_crewai=False,
        database_path=tmp_path / "api-capacity.sqlite3", output_dir=tmp_path / "outputs",
        api_key="test-verification-key-0123456789", max_active_tasks=1,
    )
    app = create_app(settings=settings, workflow=build_workflow(settings))
    assert app.state.verity_task_slots.acquire(blocking=False)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/agent/run",
            json={"task_id": "capacity-reject-01", "objective": "Verify that saturated task admission is bounded."},
            headers={"X-API-Key": settings.api_key},
        )
        assert response.status_code == 429
        assert response.headers["retry-after"] == "5"
        assert app.state.verity_store.get("capacity-reject-01") is None
    finally:
        app.state.verity_task_slots.release()


def test_failed_status_preserves_error_and_partial_checkpoint(tmp_path):
    settings = replace(
        Settings.from_env(), dry_run_test_mode=True, research_enabled=False, enable_crewai=False,
        database_path=tmp_path / "api-failed.sqlite3", output_dir=tmp_path / "outputs",
        api_key="test-verification-key-0123456789",
    )

    class FailingWorkflow:
        def invoke(self, initial_state, config=None):
            raise RuntimeError("provider returned internal diagnostic data")

        def get_state(self, config):
            return SimpleNamespace(values={
                "task_id": "api-failed-01", "status": "researching", "retry_count": 1,
                "execution_artifacts": [{"path": "partial/evidence.json"}],
            })

    app = create_app(settings=settings, workflow=FailingWorkflow())
    client = TestClient(app)
    headers = {"X-API-Key": settings.api_key}
    response = client.post(
        "/api/v1/agent/run",
        json={"task_id": "api-failed-01", "objective": "Verify failure responses preserve partial workflow context."},
        headers=headers,
    )
    assert response.status_code == 202
    result = client.get("/api/v1/agent/status/api-failed-01", headers=headers).json()
    assert result["status"] == "failed"
    assert "RuntimeError" in result["error"]
    assert "internal diagnostic data" not in result["error"]
    assert result["retry_count"] == 1
    assert result["artifacts"] == [{"path": "partial/evidence.json"}]
