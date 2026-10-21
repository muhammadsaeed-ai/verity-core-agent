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
