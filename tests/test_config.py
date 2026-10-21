from __future__ import annotations

import pytest

from src.config import Settings


def test_invalid_boolean_setting_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("VERITY_RESEARCH_ENABLED", "tru")
    with pytest.raises(ValueError, match="VERITY_RESEARCH_ENABLED"):
        Settings.from_env()


def test_api_port_must_be_valid(monkeypatch) -> None:
    monkeypatch.setenv("VERITY_API_PORT", "70000")
    with pytest.raises(ValueError, match="VERITY_API_PORT"):
        Settings.from_env()



def test_short_api_key_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("VERITY_API_KEY", "short")
    with pytest.raises(ValueError, match="at least 32 characters"):
        Settings.from_env()


def test_active_task_limit_rejects_too_high(monkeypatch):
    monkeypatch.setenv('VERITY_MAX_ACTIVE_TASKS', '33')
    with pytest.raises(ValueError):
        Settings.from_env()


def test_model_timeout_is_bounded(monkeypatch):
    monkeypatch.setenv('VERITY_MODEL_TIMEOUT_SECONDS', '4')
    with pytest.raises(ValueError, match='VERITY_MODEL_TIMEOUT_SECONDS'):
        Settings.from_env()
