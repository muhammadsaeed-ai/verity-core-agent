"""Browser Use subprocess bridge plus Playwright and HTTP search fallback."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Type
from urllib.parse import parse_qs, quote_plus, urlparse, urlunparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from src.config import Settings

logger = logging.getLogger(__name__)

try:
    from crewai.tools import BaseTool
except ImportError:
    class BaseTool:
        def __init__(self, **kwargs: Any) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)


class BrowserSearchInput(BaseModel):
    query: str = Field(min_length=2, max_length=500)


def _normalize_url(href: str) -> str | None:
    """Normalize public web URLs and reject local, credentialed, or non-web targets."""
    if not href:
        return None
    try:
        href = href.strip()
        if href.startswith("//"):
            href = "https:" + href
        parsed = urlparse(href)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if not hostname:
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        if port not in {None, 80, 443}:
            return None
        blocked_suffixes = (
            ".localhost", ".local", ".internal", ".lan", ".home",
            ".test", ".example", ".invalid",
        )
        if hostname == "localhost" or hostname.endswith(blocked_suffixes):
            return None
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if "." not in hostname:
                return None
            try:
                hostname.encode("idna").decode("ascii")
            except UnicodeError:
                return None
        else:
            if not address.is_global:
                return None

        if hostname == "duckduckgo.com" or hostname.endswith(".duckduckgo.com"):
            if parsed.path.startswith("/l/"):
                target = parse_qs(parsed.query).get("uddg", [None])[0]
                if target:
                    return _normalize_url(target)
        return urlunparse((parsed.scheme.lower(), parsed.netloc, parsed.path, "", parsed.query, ""))
    except (ValueError, UnicodeError):
        return None


def _parse_ddg_html(html: str, limit: int = 8) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, str]] = []
    for result in soup.select(".result"):
        anchor = result.select_one("a.result__a")
        if anchor is None:
            continue
        url = _normalize_url(str(anchor.get("href", "")))
        title = anchor.get_text(" ", strip=True)
        snippet_node = result.select_one(".result__snippet")
        snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
        if url and title:
            items.append({"title": title[:500], "url": url, "snippet": snippet[:4000]})
        if len(items) >= limit:
            break
    return items


class BrowserUseTool(BaseTool):
    name: str = "verity_web_research"
    description: str = (
        "Search the public web for a query. Returns JSON with titles, URLs, and snippets. "
        "Cite URLs actually returned by the browser; never invent a source."
    )
    args_schema: Type[BaseModel] = BrowserSearchInput
    settings: Any = None

    def __init__(self, settings: Settings | None = None, **kwargs: Any):
        chosen = settings or Settings.from_env()
        try:
            super().__init__(settings=chosen, **kwargs)
        except TypeError:
            super().__init__(**kwargs)
            self.settings = chosen

    def _run(self, query: str) -> str:
        return json.dumps(self.search(query), ensure_ascii=False)

    def search(self, query: str) -> dict[str, Any]:
        query = query.strip()[:500].rstrip()
        if len(query) < 2:
            return {"query": query, "results": [], "method": "validation"}
        methods = []
        if self.settings.browser_engine == "browser-use":
            methods.append(("browser-use", self._browser_use_search))
        methods.extend([("playwright", self._playwright_search), ("duckduckgo-html", self._http_search)])
        for name, method in methods:
            try:
                results = method(query)
                unique: dict[str, dict[str, str]] = {}
                for item in results or []:
                    normalized = _normalize_url(item.get("url", ""))
                    title = item.get("title", "").strip()
                    if normalized and title:
                        unique.setdefault(normalized, {
                            "title": title[:500], "url": normalized, "snippet": item.get("snippet", "")[:4000],
                        })
                if unique:
                    return {
                        "query": query, "results": list(unique.values())[:8], "method": name,
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    }
            except Exception as exc:
                logger.info("Search method %s unavailable: %s", name, exc)
        return {
            "query": query, "results": [], "method": "none",
            "warning": "Configured search methods were unavailable; no source claims were fabricated.",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }

    def _browser_use_search(self, query: str) -> list[dict[str, str]]:
        """Run Browser Use in its own Python environment to isolate its MCP dependency tree."""
        repo_root = Path(__file__).resolve().parents[2]
        configured = os.getenv("VERITY_BROWSER_USE_PYTHON", "").strip()
        candidates: list[Path] = [Path(configured)] if configured else []
        if os.name == "nt":
            candidates.append(repo_root / ".venv-browser-use" / "Scripts" / "python.exe")
        else:
            candidates.append(repo_root / ".venv-browser-use" / "bin" / "python")
        interpreter = next((path for path in candidates if path.is_file()), None)
        if interpreter is None:
            raise RuntimeError(
                "Browser Use's isolated environment is missing. Install requirements-browser-use.txt "
                "inside .venv-browser-use or configure VERITY_BROWSER_USE_PYTHON."
            )
        worker = repo_root / "scripts" / "browser_use_worker.py"
        payload = {
            "query": query,
            "provider": self.settings.provider,
            "gemini_model": self.settings.gemini_model,
            "google_api_key": self.settings.google_api_key,
            "ollama_model": self.settings.ollama_model,
            "ollama_base_url": self.settings.ollama_base_url,
            "timeout_seconds": self.settings.browser_timeout_seconds,
        }
        try:
            completed = subprocess.run(
                [str(interpreter), str(worker)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=self.settings.browser_timeout_seconds + 15,
                check=False,
                cwd=str(repo_root),
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("Browser Use worker exceeded its time limit.") from exc
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr.strip() or "Browser Use worker failed.")[-1500:])
        response = json.loads(completed.stdout)
        values = response.get("results", []) if isinstance(response, dict) else response
        if not isinstance(values, list):
            return []
        return [
            {"title": str(item.get("title", "")), "url": str(item.get("url", "")), "snippet": str(item.get("snippet", ""))}
            for item in values if isinstance(item, dict)
        ]

    def _playwright_search(self, query: str) -> list[dict[str, str]]:
        from playwright.sync_api import sync_playwright

        target = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_default_timeout(self.settings.browser_timeout_seconds * 1000)
                page.goto(target, wait_until="domcontentloaded", timeout=self.settings.browser_timeout_seconds * 1000)
                nodes = page.locator(".result").evaluate_all(
                    """items => items.map(item => {
                      const a = item.querySelector('a.result__a');
                      const s = item.querySelector('.result__snippet');
                      return a ? {title: a.innerText || a.textContent || '', url: a.href || '',
                                  snippet: s ? (s.innerText || s.textContent || '') : ''} : null;
                    }).filter(Boolean).slice(0, 8)"""
                )
                return [
                    {"title": str(item.get("title", "")), "url": str(item.get("url", "")), "snippet": str(item.get("snippet", ""))}
                    for item in nodes if isinstance(item, dict)
                ]
            finally:
                browser.close()

    def _http_search(self, query: str) -> list[dict[str, str]]:
        target = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
        request = Request(target, headers={"User-Agent": "Verity-1/1.0"})
        with urlopen(request, timeout=self.settings.browser_timeout_seconds) as response:
            html = response.read(2_500_000).decode("utf-8", errors="replace")
        return _parse_ddg_html(html)
