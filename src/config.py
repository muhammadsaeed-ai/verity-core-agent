"""Environment-backed settings and model providers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv(override=False)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean (true/false, yes/no, on/off, or 1/0).")


@dataclass(frozen=True)
class Settings:
    provider: str = "gemini"
    gemini_model: str = "gemini-2.5-flash"
    google_api_key: str = ""
    ollama_model: str = "llama3.2"
    ollama_base_url: str = "http://localhost:11434"
    dry_run_test_mode: bool = False
    browser_engine: str = "playwright"
    browser_use_network_isolated: bool = False
    research_enabled: bool = True
    enable_crewai: bool = True
    browser_timeout_seconds: int = 15
    database_path: Path = Path("data/verity.sqlite3")
    output_dir: Path = Path("outputs")
    n8n_webhook_url: str = ""
    api_key: str = ""
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    @classmethod
    def from_env(cls) -> "Settings":
        key = os.getenv("GOOGLE_API_KEY", "").strip() or os.getenv("GEMINI_API_KEY", "").strip()
        if key:
            os.environ.setdefault("GOOGLE_API_KEY", key)
            os.environ.setdefault("GEMINI_API_KEY", key)
        provider = os.getenv("VERITY_PROVIDER", "gemini").strip().lower()
        if provider not in {"gemini", "ollama"}:
            raise ValueError("VERITY_PROVIDER must be 'gemini' or 'ollama'.")
        engine = os.getenv("VERITY_BROWSER_ENGINE", "playwright").strip().lower()
        if engine not in {"playwright", "browser-use"}:
            raise ValueError("VERITY_BROWSER_ENGINE must be 'playwright' or 'browser-use'.")
        api_port = int(os.getenv("VERITY_API_PORT", "8000"))
        if not 1 <= api_port <= 65535:
            raise ValueError("VERITY_API_PORT must be between 1 and 65535.")
        api_key = os.getenv("VERITY_API_KEY", "").strip()
        if api_key and len(api_key) < 32:
            raise ValueError("VERITY_API_KEY must contain at least 32 characters.")
        return cls(
            provider=provider,
            gemini_model=os.getenv("VERITY_GEMINI_MODEL", "gemini-2.5-flash").strip(),
            google_api_key=key,
            ollama_model=os.getenv("VERITY_OLLAMA_MODEL", "llama3.2").strip(),
            ollama_base_url=os.getenv("VERITY_OLLAMA_BASE_URL", "http://localhost:11434").strip(),
            dry_run_test_mode=_env_bool("DRY_RUN_TEST_MODE", False),
            browser_engine=engine,
            browser_use_network_isolated=_env_bool("VERITY_BROWSER_USE_NETWORK_ISOLATED", False),
            research_enabled=_env_bool("VERITY_RESEARCH_ENABLED", True),
            enable_crewai=_env_bool("VERITY_ENABLE_CREWAI", True),
            browser_timeout_seconds=max(3, min(60, int(os.getenv("VERITY_BROWSER_TIMEOUT_SECONDS", "15")))),
            database_path=Path(os.getenv("VERITY_DATABASE_PATH", "data/verity.sqlite3")),
            output_dir=Path(os.getenv("VERITY_OUTPUT_DIR", "outputs")),
            n8n_webhook_url=os.getenv("N8N_WEBHOOK_URL", "").strip(),
            api_key=api_key,
            api_host=os.getenv("VERITY_API_HOST", "0.0.0.0").strip(),
            api_port=api_port,
        )


def build_chat_model(settings: Settings | None = None) -> Any:
    chosen = settings or Settings.from_env()
    if chosen.provider == "gemini":
        key = chosen.google_api_key or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError(
                "Gemini is selected but GOOGLE_API_KEY is empty. Add a Google AI Studio key "
                "to .env, or set VERITY_PROVIDER=ollama for local inference."
            )
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError as exc:
            raise RuntimeError("Install requirements.txt to enable Gemini.") from exc
        return ChatGoogleGenerativeAI(model=chosen.gemini_model, google_api_key=key, temperature=0)
    try:
        from langchain_ollama import ChatOllama
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt to enable Ollama.") from exc
    return ChatOllama(model=chosen.ollama_model, base_url=chosen.ollama_base_url, temperature=0)


def ask_model(prompt: str, settings: Settings | None = None) -> str:
    response = build_chat_model(settings).invoke(prompt)
    content = getattr(response, "content", response)
    if isinstance(content, list):
        parts = []
        for part in content:
            parts.append(str(part.get("text", "")) if isinstance(part, dict) else str(part))
        return "\n".join(part for part in parts if part)
    return str(content)
