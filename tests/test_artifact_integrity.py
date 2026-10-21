from __future__ import annotations

import hashlib
import time
from dataclasses import replace

from fastapi.testclient import TestClient

from src.api.integrity import verify_artifact_records
from src.api.server import create_app
from src.config import Settings
from src.graph.workflow import build_workflow


API_KEY = "test-verification-key-0123456789"

def _wait_for_terminal(client: TestClient, task_id: str, headers: dict[str, str], timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/agent/status/{task_id}", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in {"completed", "needs_review", "failed"}:
            return payload
        time.sleep(0.025)
    raise AssertionError(f"Task {task_id} did not finish within {timeout} seconds")



def _record(task_id: str, filename: str, data: bytes) -> dict[str, object]:
    return {
        "path": f"{task_id}/{filename}",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "media_type": "application/octet-stream",
    }


def test_artifact_integrity_verifies_digest_and_byte_count(tmp_path):
    task_id = "integrity-task-01"
    payload = b"verifiable artifact bytes"
    directory = tmp_path / task_id
    directory.mkdir()
    (directory / "report.bin").write_bytes(payload)

    result = verify_artifact_records(tmp_path, task_id, [_record(task_id, "report.bin", payload)])

    assert result["integrity_verified"] is True
    assert result["checked_count"] == result["verified_count"] == 1
    assert result["failed_count"] == 0
    assert result["artifacts"][0]["status"] == "verified"
    assert "content" not in result["artifacts"][0]


def test_artifact_integrity_detects_tampered_file_and_incorrect_size(tmp_path):
    task_id = "integrity-task-02"
    directory = tmp_path / task_id
    directory.mkdir()
    (directory / "report.bin").write_bytes(b"tampered content")
    record = _record(task_id, "report.bin", b"original content")

    result = verify_artifact_records(tmp_path, task_id, [record])

    assert result["integrity_verified"] is False
    assert result["failed_count"] == 1
    assert result["artifacts"][0]["status"] == "mismatch"


def test_artifact_integrity_rejects_traversal_and_windows_separators(tmp_path):
    task_id = "integrity-task-03"
    outside = tmp_path / "private.txt"
    outside.write_text("not an artifact", encoding="utf-8")

    result = verify_artifact_records(
        tmp_path,
        task_id,
        [
            {"path": f"{task_id}/../private.txt", "sha256": "0" * 64, "size_bytes": 15},
            {"path": f"{task_id}\\private.txt", "sha256": "0" * 64, "size_bytes": 15},
        ],
    )

    assert result["integrity_verified"] is False
    assert [item["status"] for item in result["artifacts"]] == ["invalid_path", "invalid_path"]
    assert outside.read_text(encoding="utf-8") == "not an artifact"


def test_artifact_integrity_rejects_symlinked_file_when_supported(tmp_path):
    task_id = "integrity-task-04"
    directory = tmp_path / task_id
    directory.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"private")
    link = directory / "report.bin"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        return

    result = verify_artifact_records(tmp_path, task_id, [_record(task_id, "report.bin", b"private")])
    assert result["integrity_verified"] is False
    assert result["artifacts"][0]["status"] == "rejected_symlink"


def test_integrity_endpoint_verifies_then_detects_tampering(tmp_path):
    settings = replace(
        Settings.from_env(),
        dry_run_test_mode=True,
        research_enabled=False,
        enable_crewai=False,
        database_path=tmp_path / "integrity-api.sqlite3",
        output_dir=tmp_path / "outputs",
        api_key=API_KEY,
    )
    client = TestClient(create_app(settings=settings, workflow=build_workflow(settings)))
    headers = {"X-API-Key": API_KEY}
    task_id = "integrity-endpoint-01"

    with client:
        response = client.post(
            "/api/v1/agent/run",
            headers=headers,
            json={
                "task_id": task_id,
                "objective": "Generate multiple artifacts and verify their stored checksums.",
            },
        )
        assert response.status_code == 202
        status = _wait_for_terminal(client, task_id, headers)
        assert status["status"] == "completed"

        check = client.get(f"/api/v1/agent/status/{task_id}/artifacts/verify", headers=headers)
        assert check.status_code == 200
        body = check.json()
        assert body["integrity_verified"] is True
        assert body["checked_count"] >= 5
        assert body["verified_count"] == body["checked_count"]

        report_path = settings.output_dir / task_id / "report.md"
        report_path.write_text("intentionally modified", encoding="utf-8")
        tampered = client.get(f"/api/v1/agent/status/{task_id}/artifacts/verify", headers=headers)
        assert tampered.status_code == 200
        after = tampered.json()
        assert after["integrity_verified"] is False
        assert any(
            item["path"] == f"{task_id}/report.md" and item["status"] == "mismatch"
            for item in after["artifacts"]
        )

        assert client.get(f"/api/v1/agent/status/{task_id}/artifacts/verify").status_code == 401
