"""Tests for explicit rendered extraction lanes: Earthglass + stealth CDP."""

from __future__ import annotations

import json

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
    async def fake_sleep(delay: float):
        return None

    monkeypatch.setattr("plugins.web.rendered_cdp.asyncio.sleep", fake_sleep)

    captured = await provider._capture_url("https://example.com/", format="text")

    assert captured["final_url"] == "https://example.com/"
    assert captured["title"] == "Example Domain"


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

    monkeypatch.setattr("plugins.web.rendered_cdp.fetch_json", fake_fetch_json)
    monkeypatch.setattr(provider, "_create_target_via_browser_websocket", fake_create_target)

    import asyncio

    websocket = asyncio.run(provider._resolve_target_websocket("https://example.com/", use_active_tab=False))

    assert websocket == "ws://page-1"
    assert calls == [
        ("http://127.0.0.1:49225/json/version?fingerprint=hermes-spike-20260608", "GET"),
        ("http://127.0.0.1:49225/json?fingerprint=hermes-spike-20260608", "GET"),
    ]


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
