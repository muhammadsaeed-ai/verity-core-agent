"""Browser Use + Playwright search tool with a DuckDuckGo HTTP fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from datetime import datetime, timezone
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
    if not href:
        return None
    href = href.strip()
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [None])[0]
        if target:
            href = target
            parsed = urlparse(href)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))


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
        query = query.strip()
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
        from browser_use import Agent, ChatGoogle, ChatOllama

        if self.settings.provider == "gemini":
            if not self.settings.google_api_key:
                raise RuntimeError("GOOGLE_API_KEY is required for Browser Use Gemini mode.")
            llm = ChatGoogle(model=self.settings.gemini_model, api_key=self.settings.google_api_key)
        else:
            llm = ChatOllama(model=self.settings.ollama_model, host=self.settings.ollama_base_url)

        async def run_agent() -> str:
            agent = Agent(
                task=(
                    "Search the web for this query: " + query +
                    ". Open actual search results and return ONLY a JSON array with title, url, and snippet. "
                    "Use real URLs observed in the browser; do not guess URLs."
                ),
                llm=llm, max_steps=4,
            )
            history = await agent.run()
            return str(history.final_result() or "")

        try:
            output = asyncio.run(run_agent())
        except RuntimeError as exc:
            if "asyncio.run()" not in str(exc):
                raise
            results: list[str] = []
            errors: list[BaseException] = []

            def worker() -> None:
                try:
                    results.append(asyncio.run(run_agent()))
                except BaseException as inner:
                    errors.append(inner)

            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            thread.join(timeout=self.settings.browser_timeout_seconds + 10)
            if thread.is_alive():
                raise TimeoutError("Browser Use exceeded its time budget.")
            if errors:
                raise RuntimeError(str(errors[0]))
            output = results[0] if results else ""
        match = re.search(r"\[[\s\S]*\]", output)
        if not match:
            return []
        decoded = json.loads(match.group(0))
        if not isinstance(decoded, list):
            return []
        return [
            {"title": str(item.get("title", "")), "url": str(item.get("url", "")), "snippet": str(item.get("snippet", ""))}
            for item in decoded if isinstance(item, dict)
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
