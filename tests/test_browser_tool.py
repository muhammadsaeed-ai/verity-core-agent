from __future__ import annotations

from src.tools.browser_tool import _normalize_url


def test_url_normalizer_rejects_local_and_non_web_targets() -> None:
    for url in (
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://localhost/admin",
        "http://127.0.0.1:8000/docs",
        "http://127.1/admin",
        "http://127.0.0x1/admin",
        "http://0x7f.0x0.0x0.0x1/admin",
        "http://0177.0.0.1/admin",
        "http://256.256.256.256/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://10.10.0.1/internal",
        "http://printer.local/status",
        "https://user:password@example.com/path",
        "https://example.com:8443/path",
    ):
        assert _normalize_url(url) is None, url


def test_url_normalizer_keeps_public_http_urls_and_unwraps_search_redirects() -> None:
    assert _normalize_url("https://example.com/article#fragment") == "https://example.com/article"
    assert _normalize_url(
        "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Farticle"
    ) == "https://example.com/article"
    assert _normalize_url("https://duckduckgo.com/l/?uddg=http%3A%2F%2F127.0.0.1%2F") is None



def test_browser_use_requires_network_egress_isolation(monkeypatch, tmp_path) -> None:
    from dataclasses import replace

    import pytest

    from src.config import Settings
    from src.tools.browser_tool import BrowserUseTool

    settings = replace(
        Settings.from_env(), browser_engine="browser-use", browser_use_network_isolated=False
    )
    tool = BrowserUseTool(settings=settings)
    with pytest.raises(RuntimeError, match="blocked by default"):
        tool._browser_use_search("test query")
