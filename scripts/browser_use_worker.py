"""Dedicated-environment worker for Browser Use.

Reads a JSON request from stdin and writes a JSON response to stdout.
This is installed with requirements-browser-use.txt, not the main CrewAI environment.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any


def _extract_results(value: Any) -> list[dict[str, str]]:
    if isinstance(value, dict):
        if isinstance(value.get("results"), list):
            value = value["results"]
        else:
            value = value.get("content", value)
    if isinstance(value, str):
        match = re.search(r"\[[\s\S]*\]", value)
        if not match:
            return []
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    results = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        url = str(item.get("url", "")).strip()
        snippet = str(item.get("snippet", "")).strip()
        if title and url.startswith(("https://", "http://")):
            results.append({"title": title[:500], "url": url, "snippet": snippet[:4000]})
    return results[:8]


async def _run(payload: dict[str, Any]) -> list[dict[str, str]]:
    from browser_use import Agent

    provider = str(payload.get("provider", "gemini")).lower()
    if provider == "gemini":
        from browser_use import ChatGoogle
        key = str(payload.get("google_api_key", "")).strip()
        if not key:
            raise RuntimeError("GOOGLE_API_KEY is required for Browser Use in Gemini mode.")
        llm = ChatGoogle(
            model=str(payload.get("gemini_model", "gemini-2.5-flash")),
            api_key=key,
        )
    elif provider == "ollama":
        from browser_use import ChatOllama
        llm = ChatOllama(
            model=str(payload.get("ollama_model", "llama3.2")),
            host=str(payload.get("ollama_base_url", "http://localhost:11434")),
        )
    else:
        raise ValueError("Unsupported provider; choose gemini or ollama.")

    query = str(payload.get("query", "")).strip()
    agent = Agent(
        task=(
            "Search the public web for this query: " + query +
            ". Visit actual result pages and return only a JSON array of objects with title, url, and snippet. "
            "Use URLs observed in the browser. Never invent sources or assert that search snippets prove a claim."
        ),
        llm=llm,
        max_steps=4,
    )
    history = await agent.run()
    final_value = history.final_result()
    return _extract_results(final_value)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        results = asyncio.run(_run(payload))
        print(json.dumps({"results": results}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
