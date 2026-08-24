"""Tests for explicit rendered extraction lanes: Earthglass + stealth CDP."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_earthglass_provider_extracts_authenticated_html_from_cdp(monkeypatch):
    from plugins.web.earthglass.provider import EarthglassWebExtractProvider

    provider = EarthglassWebExtractProvider()
    monkeypatch.setenv("EARTHGLASS_CDP_URL", "http://127.0.0.1:9223")

    async def fake_capture(url: str, *, format: str, use_active_tab: bool = False):
        assert url == "https://example.com/product"
        assert format == "html"
        assert use_active_tab is False
        return {
            "url": url,
            "final_url": "https://example.com/product?selectedStore=123",
            "title": "Example Product",
            "html": "<html><body><h1>Example Product</h1><span>$3.48</span></body></html>",
            "text": "Example Product\n$3.48",
        }

    monkeypatch.setattr(provider, "_capture_url", fake_capture)

    results = await provider.extract(["https://example.com/product"], format="html")

    assert results == [
        {
            "url": "https://example.com/product?selectedStore=123",
            "title": "Example Product",
            "content": "<html><body><h1>Example Product</h1><span>$3.48</span></body></html>",
            "raw_content": "<html><body><h1>Example Product</h1><span>$3.48</span></body></html>",
            "metadata": {
                "backend": "earthglass",
                "lane": "authenticated",
                "authenticated": True,
                "source_url": "https://example.com/product",
                "capture_format": "html",
            },
        }
    ]


@pytest.mark.asyncio
async def test_cdp_capture_retries_until_new_target_finishes_loading(monkeypatch):
    from plugins.web.earthglass.provider import EarthglassWebExtractProvider

    provider = EarthglassWebExtractProvider()
    monkeypatch.setenv("EARTHGLASS_CDP_URL", "http://127.0.0.1:9223")
    async def fake_resolve_target_websocket(url: str, use_active_tab: bool = False):
        return "ws://page"

    monkeypatch.setattr(provider, "_resolve_target_websocket", fake_resolve_target_websocket)

    captures = iter([
        {"url": "about:blank", "title": "", "html": "<html></html>", "text": ""},
        {"url": "https://example.com/", "title": "Example Domain", "html": "<h1>Example Domain</h1>", "text": "Example Domain"},
    ])

    async def fake_evaluate(websocket_url: str, expression: str):
        assert websocket_url == "ws://page"
        return next(captures)

    monkeypatch.setattr(provider, "_evaluate", fake_evaluate)

    captured = await provider._capture_url("https://example.com/", format="text")

    assert captured["final_url"] == "https://example.com/"
    assert captured["title"] == "Example Domain"


@pytest.mark.asyncio
async def test_cdp_capture_closes_owned_target_after_success(monkeypatch):
    from plugins.web.cloakbrowser_acubens.provider import CloakBrowserAcubensWebExtractProvider

    provider = CloakBrowserAcubensWebExtractProvider()
    target = SimpleNamespace(
        websocket_url="ws://page",
        target_id="target-1",
        browser_websocket_url="ws://browser",
        owned=True,
    )
    closed = []

    async def fake_resolve(url: str, *, use_active_tab: bool):
        return target

    async def fake_evaluate(websocket_url, expression: str):
        assert getattr(websocket_url, "websocket_url", websocket_url) == "ws://page"
        return {
            "url": "https://example.com/",
            "title": "Example Domain",
            "html": "<h1>Example Domain</h1>",
            "text": "Example Domain",
        }

    async def fake_close(resolved_target):
        closed.append(resolved_target)

    monkeypatch.setattr(provider, "_resolve_target_websocket", fake_resolve)
    monkeypatch.setattr(provider, "_evaluate", fake_evaluate)
    monkeypatch.setattr(provider, "_close_target", fake_close, raising=False)

    captured = await provider._capture_url("https://example.com/", format="text")

    assert captured["title"] == "Example Domain"
    assert closed == [target]


@pytest.mark.asyncio
async def test_cdp_capture_closes_owned_target_after_evaluation_failure(monkeypatch):
    from plugins.web.cloakbrowser_acubens.provider import CloakBrowserAcubensWebExtractProvider

    provider = CloakBrowserAcubensWebExtractProvider()
    target = SimpleNamespace(
        websocket_url="ws://page",
        target_id="target-1",
        browser_websocket_url="ws://browser",
        owned=True,
    )
    closed = []

    async def fake_resolve(url: str, *, use_active_tab: bool):
        return target

    async def fake_evaluate(websocket_url, expression: str):
        raise RuntimeError("renderer disconnected")

    async def fake_close(resolved_target):
        closed.append(resolved_target)

    monkeypatch.setattr(provider, "_resolve_target_websocket", fake_resolve)
    monkeypatch.setattr(provider, "_evaluate", fake_evaluate)
    monkeypatch.setattr(provider, "_close_target", fake_close, raising=False)

    with pytest.raises(RuntimeError, match="renderer disconnected"):
        await provider._capture_url("https://example.com/", format="text")

    assert closed == [target]


@pytest.mark.asyncio
async def test_cdp_capture_closes_owned_target_after_capture_timeout(monkeypatch):
    from plugins.web.cloakbrowser_acubens.provider import CloakBrowserAcubensWebExtractProvider

    provider = CloakBrowserAcubensWebExtractProvider()
    provider.capture_timeout_seconds = 0.01
    target = SimpleNamespace(
        websocket_url="ws://page",
        target_id="target-1",
        browser_websocket_url="ws://browser",
        owned=True,
    )
    closed = []

    async def fake_resolve(url: str, *, use_active_tab: bool):
        return target

    async def fake_evaluate(websocket_url, expression: str):
        await asyncio.Event().wait()

    async def fake_close(resolved_target):
        closed.append(resolved_target)

    monkeypatch.setattr(provider, "_resolve_target_websocket", fake_resolve)
    monkeypatch.setattr(provider, "_evaluate", fake_evaluate)
    monkeypatch.setattr(provider, "_close_target", fake_close, raising=False)

    with pytest.raises(TimeoutError, match="Timed out capturing https://example.com/"):
        await asyncio.wait_for(
            provider._capture_url("https://example.com/", format="text"),
            timeout=0.1,
        )

    assert closed == [target]


@pytest.mark.asyncio
async def test_cdp_capture_shields_close_and_reraises_caller_cancellation(monkeypatch):
    from plugins.web.cloakbrowser_acubens.provider import CloakBrowserAcubensWebExtractProvider

    provider = CloakBrowserAcubensWebExtractProvider()
    target = SimpleNamespace(
        websocket_url="ws://page",
        target_id="target-1",
        browser_websocket_url="ws://browser",
        owned=True,
    )
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    close_completed = asyncio.Event()

    async def fake_resolve(url: str, *, use_active_tab: bool):
        return target

    async def fake_evaluate(websocket_url: str, expression: str):
        raise RuntimeError("older capture failure")

    async def fake_close(resolved_target):
        close_started.set()
        await allow_close.wait()
        close_completed.set()

    monkeypatch.setattr(provider, "_resolve_target_websocket", fake_resolve)
    monkeypatch.setattr(provider, "_evaluate", fake_evaluate)
    monkeypatch.setattr(provider, "_close_target", fake_close)

    capture = asyncio.create_task(provider._capture_url("https://example.com/", format="text"))
    await close_started.wait()
    capture.cancel()
    await asyncio.sleep(0)
    allow_close.set()

    with pytest.raises(asyncio.CancelledError):
        await capture
    assert close_completed.is_set()


@pytest.mark.asyncio
async def test_cdp_capture_cancellation_waits_only_for_close_hard_deadline(monkeypatch):
    from plugins.web.cloakbrowser_acubens.provider import CloakBrowserAcubensWebExtractProvider

    provider = CloakBrowserAcubensWebExtractProvider()
    provider.cdp_close_timeout_seconds = 0.01
    target = SimpleNamespace(
        websocket_url="ws://page",
        target_id="target-1",
        browser_websocket_url="ws://browser",
        owned=True,
    )
    close_started = asyncio.Event()
    close_cancelled = asyncio.Event()

    async def fake_resolve(url: str, *, use_active_tab: bool):
        return target

    async def fake_evaluate(websocket_url: str, expression: str):
        raise RuntimeError("older capture failure")

    async def fake_close(resolved_target):
        close_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            close_cancelled.set()

    monkeypatch.setattr(provider, "_resolve_target_websocket", fake_resolve)
    monkeypatch.setattr(provider, "_evaluate", fake_evaluate)
    monkeypatch.setattr(provider, "_close_target", fake_close)

    capture = asyncio.create_task(provider._capture_url("https://example.com/", format="text"))
    await close_started.wait()
    capture.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(capture, timeout=0.1)
    await asyncio.wait_for(close_cancelled.wait(), timeout=0.1)


@pytest.mark.asyncio
async def test_cdp_capture_does_not_close_active_user_target(monkeypatch):
    from plugins.web.earthglass.provider import EarthglassWebExtractProvider

    provider = EarthglassWebExtractProvider()
    target = SimpleNamespace(
        websocket_url="ws://active-page",
        target_id="user-target",
        browser_websocket_url="ws://browser",
        owned=False,
    )
    closed = []

    async def fake_resolve(url: str, *, use_active_tab: bool):
        assert use_active_tab is True
        return target

    async def fake_evaluate(websocket_url, expression: str):
        assert getattr(websocket_url, "websocket_url", websocket_url) == "ws://active-page"
        return {
            "url": "https://example.com/account",
            "title": "Account",
            "html": "<h1>Account</h1>",
            "text": "Account",
        }

    async def fake_close(resolved_target):
        closed.append(resolved_target)

    monkeypatch.setattr(provider, "_resolve_target_websocket", fake_resolve)
    monkeypatch.setattr(provider, "_evaluate", fake_evaluate)
    monkeypatch.setattr(provider, "_close_target", fake_close, raising=False)

    captured = await provider._capture_url(
        "https://example.com/account",
        format="text",
        use_active_tab=True,
    )

    assert captured["title"] == "Account"
    assert closed == []


@pytest.mark.asyncio
async def test_rendered_extract_total_deadline_bounds_multi_url_call(monkeypatch):
    from plugins.web.cloakbrowser_acubens.provider import CloakBrowserAcubensWebExtractProvider

    provider = CloakBrowserAcubensWebExtractProvider()
    monkeypatch.setenv("CLOAKBROWSER_CDP_URL", "http://127.0.0.1:49225?fingerprint=test")
    provider.total_extract_timeout_seconds = 0.01

    async def fake_capture(url: str, *, format: str, use_active_tab: bool = False):
        await asyncio.Event().wait()

    monkeypatch.setattr(provider, "_capture_url", fake_capture)

    results = await asyncio.wait_for(
        provider.extract(
            ["https://example.com/one", "https://example.com/two"],
            format="text",
        ),
        timeout=0.1,
    )

    assert len(results) == 2
    assert "total extraction deadline" in results[0]["error"].lower()
    assert "total extraction deadline" in results[1]["error"].lower()


@pytest.mark.asyncio
async def test_rendered_extract_validation_runs_off_loop_inside_total_deadline(monkeypatch):
    from plugins.web.cloakbrowser_acubens.provider import CloakBrowserAcubensWebExtractProvider

    provider = CloakBrowserAcubensWebExtractProvider()
    monkeypatch.setenv("CLOAKBROWSER_CDP_URL", "http://127.0.0.1:49225")
    provider.total_extract_timeout_seconds = 0.01
    validation_started = threading.Event()
    release_validation = threading.Event()

    def blocking_validation(url: str):
        validation_started.set()
        release_validation.wait(timeout=0.2)
        return None

    async def unexpected_capture(url: str, *, format: str, use_active_tab: bool = False):
        raise AssertionError("capture must not start after validation consumes the deadline")

    monkeypatch.setattr(provider, "_validate_url", blocking_validation)
    monkeypatch.setattr(provider, "_capture_url", unexpected_capture)

    started = time.perf_counter()
    try:
        results = await provider.extract(["https://example.com/"], format="text")
    finally:
        release_validation.set()
    elapsed = time.perf_counter() - started

    assert validation_started.is_set()
    assert elapsed < 0.1
    assert "total extraction deadline" in results[0]["error"].lower()


@pytest.mark.asyncio
async def test_cdp_evaluate_times_out_when_renderer_never_replies(monkeypatch):
    from plugins.web.cloakbrowser_acubens.provider import CloakBrowserAcubensWebExtractProvider

    provider = CloakBrowserAcubensWebExtractProvider()
    provider.cdp_command_timeout_seconds = 0.01

    class HangingWebSocket:
        async def send(self, payload: str):
            return None

        async def recv(self):
            await asyncio.Event().wait()

    class ConnectContext:
        async def __aenter__(self):
            return HangingWebSocket()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    fake_websockets = SimpleNamespace(connect=lambda *args, **kwargs: ConnectContext())
    monkeypatch.setitem(sys.modules, "websockets", fake_websockets)

    with pytest.raises(TimeoutError, match="Timed out waiting for response to Runtime.evaluate"):
        await asyncio.wait_for(
            provider._evaluate("ws://page", "document.title"),
            timeout=0.1,
        )


def test_query_endpoint_resolves_new_target_through_browser_websocket(monkeypatch):
    from plugins.web.cloakbrowser_acubens.provider import CloakBrowserAcubensWebExtractProvider

    provider = CloakBrowserAcubensWebExtractProvider()
    monkeypatch.setenv(
        "CLOAKBROWSER_CDP_URL",
        "http://127.0.0.1:49225?fingerprint=hermes-spike-20260608",
    )

    calls = []

    async def fake_fetch_json(url: str, *, method: str = "GET", timeout: float = 20):
        calls.append((url, method))
        if url.endswith("/json/version?fingerprint=hermes-spike-20260608"):
            return {"webSocketDebuggerUrl": "ws://browser"}
        if url.endswith("/json?fingerprint=hermes-spike-20260608"):
            return [
                {"id": "target-1", "webSocketDebuggerUrl": "ws://page-1"},
            ]
        raise AssertionError(url)

    async def fake_create_target(browser_ws: str, url: str):
        assert browser_ws == "ws://browser"
        assert url == "https://example.com/"
        return "target-1"

    monkeypatch.setattr(sys.modules["plugins.web.rendered_cdp"], "fetch_json", fake_fetch_json)
    monkeypatch.setattr(provider, "_create_target_via_browser_websocket", fake_create_target)

    import asyncio

    resolved = asyncio.run(provider._resolve_target_websocket("https://example.com/", use_active_tab=False))

    assert resolved.websocket_url == "ws://page-1"
    assert resolved.target_id == "target-1"
    assert resolved.browser_websocket_url == "ws://browser"
    assert resolved.owned is True
    assert calls == [
        ("http://127.0.0.1:49225/json/version?fingerprint=hermes-spike-20260608", "GET"),
        ("http://127.0.0.1:49225/json?fingerprint=hermes-spike-20260608", "GET"),
    ]


@pytest.mark.asyncio
async def test_query_endpoint_closes_created_target_when_target_list_resolution_fails(monkeypatch):
    from plugins.web.cloakbrowser_acubens.provider import CloakBrowserAcubensWebExtractProvider

    provider = CloakBrowserAcubensWebExtractProvider()
    monkeypatch.setenv(
        "CLOAKBROWSER_CDP_URL",
        "http://127.0.0.1:49225?fingerprint=cleanup-test",
    )
    closed = []

    async def fake_fetch_json(url: str, *, method: str = "GET", timeout: float = 20):
        if url.endswith("/json/version?fingerprint=cleanup-test"):
            return {"webSocketDebuggerUrl": "ws://browser"}
        if url.endswith("/json?fingerprint=cleanup-test"):
            return []
        raise AssertionError(url)

    async def fake_create_target(browser_ws: str, url: str):
        return "target-orphan"

    async def fake_close(target):
        closed.append(target)

    monkeypatch.setattr(sys.modules["plugins.web.rendered_cdp"], "fetch_json", fake_fetch_json)
    monkeypatch.setattr(provider, "_create_target_via_browser_websocket", fake_create_target)
    monkeypatch.setattr(provider, "_close_target", fake_close)

    with pytest.raises(RuntimeError, match="not visible"):
        await provider._resolve_target_websocket(
            "https://example.com/",
            use_active_tab=False,
        )

    assert len(closed) == 1
    assert closed[0].target_id == "target-orphan"
    assert closed[0].browser_websocket_url == "ws://browser"
    assert closed[0].owned is True


@pytest.mark.asyncio
async def test_cdp_close_requires_explicit_true_confirmation(monkeypatch):
    from plugins.web.cloakbrowser_acubens.provider import CloakBrowserAcubensWebExtractProvider
    from plugins.web.rendered_cdp import ResolvedCdpTarget

    provider = CloakBrowserAcubensWebExtractProvider()
    target = ResolvedCdpTarget(
        websocket_url="ws://page",
        target_id="target-1",
        browser_websocket_url="ws://browser",
        owned=True,
    )

    async def fake_cdp_command(websocket_url: str, method: str, params):
        return {}

    monkeypatch.setattr(provider, "_cdp_command", fake_cdp_command)

    with pytest.raises(RuntimeError, match="refused target target-1"):
        await provider._close_target(target)


def test_cdp_url_builders_match_chrome_and_preserve_fingerprint_query(monkeypatch):
    from plugins.web.cloakbrowser_acubens.provider import CloakBrowserAcubensWebExtractProvider
    from plugins.web.rendered_cdp import build_cdp_new_target_url

    assert (
        build_cdp_new_target_url("http://127.0.0.1:9223", "https://example.com/")
        == "http://127.0.0.1:9223/json/new?https%3A%2F%2Fexample.com%2F"
    )

    provider = CloakBrowserAcubensWebExtractProvider()
    monkeypatch.setenv(
        "CLOAKBROWSER_CDP_URL",
        "http://127.0.0.1:49225?fingerprint=hermes-spike-20260608",
    )

    assert provider.endpoint == "http://127.0.0.1:49225?fingerprint=hermes-spike-20260608"
    assert (
        provider.discovery_url
        == "http://127.0.0.1:49225/json/version?fingerprint=hermes-spike-20260608"
    )


@pytest.mark.asyncio
async def test_explicit_tools_route_to_separate_lanes(monkeypatch):
    from tools import rendered_web_extract_tools as tools

    calls = []

    async def fake_extract(provider_name, urls, **kwargs):
        calls.append((provider_name, urls, kwargs))
        return json.dumps({"results": [{"url": urls[0], "metadata": {"backend": provider_name}}]})

    monkeypatch.setattr(tools, "_extract_with_named_provider", fake_extract)

    auth_raw = await tools.authenticated_web_extract_tool(["https://www.walmart.com/ip/1"])
    stealth_raw = await tools.stealth_web_extract_tool(["https://www.walmart.com/ip/1"])

    assert json.loads(auth_raw)["results"][0]["metadata"]["backend"] == "earthglass"
    assert json.loads(stealth_raw)["results"][0]["metadata"]["backend"] == "cloakbrowser-acubens"
    assert calls[0][0] == "earthglass"
    assert calls[1][0] == "cloakbrowser-acubens"


@pytest.mark.asyncio
async def test_rendered_lanes_never_auto_selected_as_ordinary_backend(monkeypatch):
    """The auto-detect ladder must not route ordinary web_search/web_extract
    through the explicit rendered lanes, even when the lane is available on
    this host (script present) and nothing else is configured."""
    from agent import web_search_registry
    from plugins.web.earthglass.provider import EarthglassWebExtractProvider
    from tools import web_tools

    monkeypatch.setenv("EARTHGLASS_CDP_URL", "http://127.0.0.1:9223")
    monkeypatch.setattr(
        EarthglassWebExtractProvider, "is_available", lambda self: True, raising=True
    )
    for key in (
        "BRAVE_SEARCH_API_KEY",
        "SEARXNG_URL",
        "TAVILY_API_KEY",
        "EXA_API_KEY",
        "PARALLEL_API_KEY",
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL",
        "FIRECRAWL_GATEWAY_URL",
        "TOOL_GATEWAY_DOMAIN",
    ):
        monkeypatch.delenv(key, raising=False)

    web_search_registry._reset_for_tests()
    web_search_registry.register_provider(EarthglassWebExtractProvider())
    try:
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: False)
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
        monkeypatch.setattr(web_tools, "_firecrawl_client", None, raising=False)
        monkeypatch.setattr(web_tools, "_firecrawl_client_config", None, raising=False)
        monkeypatch.setattr(web_search_registry, "_keyless_tier_enabled", lambda: False)
        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)

        assert web_tools._get_search_backend() != "earthglass"
        assert web_tools._get_extract_backend() != "earthglass"

        # Explicit selection remains strict and unaffected by the opt-out.
        monkeypatch.setattr(
            web_tools, "_load_web_config", lambda: {"extract_backend": "earthglass"}
        )
        assert web_tools._get_extract_backend() == "earthglass"
    finally:
        web_search_registry._reset_for_tests()
