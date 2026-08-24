"""Tests for the Z.AI Web Search provider (plugins/web/zai/).

Covers:
- ZaiMcpClient — initialize handshake, session reuse, stale-session
  re-initialize + single retry, SSE vs JSON body decoding, JSON-RPC
  error surfacing, thread-safe client cache
- ZaiWebSearchProvider.is_available() — env-key gating, no network
- search() — result mapping ({title, link, content} → {title, url,
  description, position}), limit slicing, location/content_size config
  defaults (engine default region is "cn" — provider pins "us"),
  doubly-encoded / empty payloads, error envelope
- extract() — webReader JSON mapping, per-URL error isolation,
  non-JSON body passthrough
- tools.web_tools integration — "zai" accepted as configured backend,
  available when the key is present, absent from the auto-detect ladder
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Test doubles — a scripted MCP transport
# ---------------------------------------------------------------------------


class FakeMcpTransport:
    """Replaces ``ZaiMcpClient._post``. Scripts initialize → calls."""

    def __init__(self, responses=None, initialize_status=200):
        # responses: list of (status, session_id_or_None, raw_body) returned
        # for each non-initialize POST, in order.
        self.responses = list(responses or [])
        self.initialize_status = initialize_status
        self.calls: list[dict] = []
        self.initialize_count = 0

    def __call__(self, payload, session_id):
        """Bound-method patch for ``ZaiMcpClient._post(payload, session_id)``."""
        self.calls.append({"payload": payload, "session_id": session_id})
        if payload.get("method") == "initialize":
            self.initialize_count += 1
            return self.initialize_status, "sess-1" if self.initialize_status == 200 else None, "{}"
        if payload.get("method") == "notifications/initialized":
            return 200, None, ""
        status, sid, body = self.responses.pop(0) if self.responses else (200, None, "{}")
        return status, sid, body


def _rpc_result(content_text, is_error=False):
    """Build a tools/call JSON-RPC response body with one text block."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "result": {"isError": is_error, "content": [{"type": "text", "text": content_text}]},
        }
    )


def _sse_wrap(body: str) -> str:
    return f"event: message\ndata: {body}\n\n"


def _search_rows(n=2):
    return json.dumps(
        [
            {
                "title": f"Result {i}",
                "link": f"https://example.com/{i}",
                "content": f"Summary {i}",
                "refer": f"ref_{i}",
            }
            for i in range(1, n + 1)
        ]
    )


@pytest.fixture(autouse=True)
def _clean_env_and_cache(monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("Z_AI_API_KEY", raising=False)
    from plugins.web.zai import mcp_client as mc
    from plugins.web.zai import provider as prov

    mc.reset_zai_mcp_clients()
    monkeypatch.setattr(prov, "_load_zai_web_config", lambda: {})
    yield
    mc.reset_zai_mcp_clients()


def _keyed_transport(monkeypatch, responses=None, initialize_status=200):
    """Patch the provider key + MCP transport. Returns the scripted transport."""
    from plugins.web.zai import mcp_client as mc
    from plugins.web.zai import provider as prov

    monkeypatch.setattr(prov, "_resolve_api_key", lambda: "test-key")
    transport = FakeMcpTransport(responses, initialize_status)
    monkeypatch.setattr(mc.ZaiMcpClient, "_post", transport.__call__)
    return transport


# ---------------------------------------------------------------------------
# ZaiMcpClient
# ---------------------------------------------------------------------------


class TestZaiMcpClient:
    def test_initialize_handshake_and_session_reuse(self, monkeypatch):
        from plugins.web.zai.mcp_client import ZaiMcpClient

        transport = _keyed_transport(
            monkeypatch,
            responses=[
                (200, "sess-1", _rpc_result("[]")),
                (200, "sess-1", _rpc_result("[]")),
            ],
        )
        client = ZaiMcpClient("https://api.z.ai/api/mcp/web_search_prime/mcp", "k")
        assert client.tool_text("web_search_prime", {"search_query": "q"}) == "[]"
        client.tool_text("web_search_prime", {"search_query": "q2"})
        # One initialize + one initialized notification for two calls.
        assert transport.initialize_count == 1
        methods = [c["payload"].get("method") for c in transport.calls]
        assert methods.count("notifications/initialized") == 1
        assert methods.count("tools/call") == 2
        # Both calls carried the session id.
        assert all(c["session_id"] == "sess-1" for c in transport.calls if c["payload"].get("method") == "tools/call")

    def test_stale_session_reinitializes_once(self, monkeypatch):
        from plugins.web.zai.mcp_client import ZaiMcpClient

        transport = _keyed_transport(
            monkeypatch,
            responses=[
                (404, None, '{"error": "session not found"}'),
                (200, "sess-2", _rpc_result('"ok"')),
            ],
        )
        client = ZaiMcpClient("https://api.z.ai/api/mcp/x/mcp", "k")
        assert client.tool_text("t", {}) == '"ok"'
        assert transport.initialize_count == 2

    def test_sse_framed_response_parsed(self, monkeypatch):
        from plugins.web.zai.mcp_client import ZaiMcpClient

        body = _rpc_result('"sse-payload"')
        _keyed_transport(monkeypatch, responses=[(200, "s", _sse_wrap(body))])
        client = ZaiMcpClient("https://api.z.ai/api/mcp/x/mcp", "k")
        assert client.tool_text("t", {}) == '"sse-payload"'

    def test_http_error_raises_zai_mcp_error(self, monkeypatch):
        from plugins.web.zai.mcp_client import ZaiMcpClient, ZaiMcpError

        _keyed_transport(monkeypatch, responses=[(429, None, '{"error":{"code":"1113","message":"Insufficient balance"}}')])
        client = ZaiMcpClient("https://api.z.ai/api/mcp/x/mcp", "k")
        with pytest.raises(ZaiMcpError) as excinfo:
            client.call_tool("t", {})
        assert "429" in str(excinfo.value)

    def test_jsonrpc_error_object_raises(self, monkeypatch):
        from plugins.web.zai.mcp_client import ZaiMcpClient, ZaiMcpError

        body = json.dumps({"jsonrpc": "2.0", "id": 99, "error": {"code": -32000, "message": "boom"}})
        _keyed_transport(monkeypatch, responses=[(200, "s", body)])
        client = ZaiMcpClient("https://api.z.ai/api/mcp/x/mcp", "k")
        with pytest.raises(ZaiMcpError, match="boom"):
            client.call_tool("t", {})

    def test_iserror_true_raises_with_text(self, monkeypatch):
        from plugins.web.zai.mcp_client import ZaiMcpClient, ZaiMcpError

        _keyed_transport(monkeypatch, responses=[(200, "s", _rpc_result("rate limited", is_error=True))])
        client = ZaiMcpClient("https://api.z.ai/api/mcp/x/mcp", "k")
        with pytest.raises(ZaiMcpError, match="rate limited"):
            client.tool_text("t", {})

    def test_client_cache_reuses_per_endpoint(self, monkeypatch):
        from plugins.web.zai.mcp_client import get_zai_mcp_client, reset_zai_mcp_clients

        reset_zai_mcp_clients()
        a = get_zai_mcp_client("https://api.z.ai/api/mcp/a/mcp", "k")
        b = get_zai_mcp_client("https://api.z.ai/api/mcp/a/mcp", "k")
        c = get_zai_mcp_client("https://api.z.ai/api/mcp/b/mcp", "k")
        assert a is b
        assert a is not c


# ---------------------------------------------------------------------------
# Provider: identity / availability
# ---------------------------------------------------------------------------


class TestZaiProviderIdentity:
    def test_provider_name(self):
        from plugins.web.zai.provider import ZaiWebSearchProvider
        assert ZaiWebSearchProvider().name == "zai"

    def test_implements_web_search_provider(self):
        from agent.web_search_provider import WebSearchProvider
        from plugins.web.zai.provider import ZaiWebSearchProvider
        assert issubclass(ZaiWebSearchProvider, WebSearchProvider)

    def test_supports_search_and_extract(self):
        from plugins.web.zai.provider import ZaiWebSearchProvider
        p = ZaiWebSearchProvider()
        assert p.supports_search() is True
        assert p.supports_extract() is True

    def test_is_available_true_with_key(self, monkeypatch):
        from plugins.web.zai.provider import ZaiWebSearchProvider
        _keyed_transport(monkeypatch)
        assert ZaiWebSearchProvider().is_available() is True

    def test_is_available_false_without_key(self):
        from plugins.web.zai.provider import ZaiWebSearchProvider
        assert ZaiWebSearchProvider().is_available() is False

    def test_setup_schema_shape(self):
        from plugins.web.zai.provider import ZaiWebSearchProvider
        schema = ZaiWebSearchProvider().get_setup_schema()
        assert schema["env_vars"][0]["key"] == "ZAI_API_KEY"
        assert schema["badge"] == "coding-plan"


# ---------------------------------------------------------------------------
# Provider: search
# ---------------------------------------------------------------------------


class TestZaiProviderSearch:
    def test_search_maps_rows(self, monkeypatch):
        from plugins.web.zai.provider import ZaiWebSearchProvider

        transport = _keyed_transport(monkeypatch, responses=[(200, "s", _rpc_result(_search_rows(2)))])
        out = ZaiWebSearchProvider().search("openai news", 5)
        assert out["success"] is True
        rows = out["data"]["web"]
        assert len(rows) == 2
        assert rows[0] == {
            "title": "Result 1",
            "url": "https://example.com/1",
            "description": "Summary 1",
            "position": 0,
        }
        # Engine region pinned to us by default.
        call = next(c for c in transport.calls if c["payload"].get("method") == "tools/call")
        args = call["payload"]["params"]["arguments"]
        assert args["location"] == "us"
        assert args["content_size"] == "medium"
        assert args["search_query"] == "openai news"

    def test_search_respects_limit(self, monkeypatch):
        from plugins.web.zai.provider import ZaiWebSearchProvider

        _keyed_transport(monkeypatch, responses=[(200, "s", _rpc_result(_search_rows(10)))])
        out = ZaiWebSearchProvider().search("q", 3)
        assert [r["position"] for r in out["data"]["web"]] == [0, 1, 2]

    def test_search_empty_result_string(self, monkeypatch):
        from plugins.web.zai.provider import ZaiWebSearchProvider

        # Observed live: empty result sets come back as the 4-char string "[]".
        _keyed_transport(monkeypatch, responses=[(200, "s", _rpc_result("[]"))])
        out = ZaiWebSearchProvider().search("obscure query", 5)
        assert out["success"] is True
        assert out["data"]["web"] == []

    def test_search_doubly_encoded_payload(self, monkeypatch):
        from plugins.web.zai.provider import ZaiWebSearchProvider

        inner = _search_rows(1)
        _keyed_transport(monkeypatch, responses=[(200, "s", _rpc_result(json.dumps(inner)))])
        out = ZaiWebSearchProvider().search("q", 5)
        assert len(out["data"]["web"]) == 1

    def test_search_location_and_content_size_config(self, monkeypatch):
        from plugins.web.zai import provider as prov
        from plugins.web.zai.provider import ZaiWebSearchProvider

        monkeypatch.setattr(prov, "_load_zai_web_config", lambda: {"location": "cn", "content_size": "high"})
        transport = _keyed_transport(monkeypatch, responses=[(200, "s", _rpc_result(_search_rows(1)))])
        ZaiWebSearchProvider().search("q", 5)
        call = next(c for c in transport.calls if c["payload"].get("method") == "tools/call")
        args = call["payload"]["params"]["arguments"]
        assert args["location"] == "cn"
        assert args["content_size"] == "high"

    def test_search_invalid_config_values_fall_back(self, monkeypatch):
        from plugins.web.zai import provider as prov
        from plugins.web.zai.provider import ZaiWebSearchProvider

        monkeypatch.setattr(prov, "_load_zai_web_config", lambda: {"location": "mars", "content_size": "gigantic"})
        transport = _keyed_transport(monkeypatch, responses=[(200, "s", _rpc_result(_search_rows(1)))])
        ZaiWebSearchProvider().search("q", 5)
        call = next(c for c in transport.calls if c["payload"].get("method") == "tools/call")
        args = call["payload"]["params"]["arguments"]
        assert args["location"] == "us"
        assert args["content_size"] == "medium"

    def test_search_error_envelope(self, monkeypatch):
        from plugins.web.zai.mcp_client import ZaiMcpError
        from plugins.web.zai.provider import ZaiWebSearchProvider

        _keyed_transport(monkeypatch, responses=[(429, None, '{"error":{"code":"1113","message":"Insufficient balance"}}')])
        out = ZaiWebSearchProvider().search("q", 5)
        assert out["success"] is False
        assert "429" in out["error"]

    def test_search_without_key(self):
        from plugins.web.zai.provider import ZaiWebSearchProvider
        out = ZaiWebSearchProvider().search("q", 5)
        assert out["success"] is False
        assert "ZAI_API_KEY" in out["error"]


# ---------------------------------------------------------------------------
# Provider: extract
# ---------------------------------------------------------------------------


class TestZaiProviderExtract:
    def _reader_payload(self):
        return json.dumps(
            {
                "title": "Example Page",
                "description": "A page about examples",
                "url": "https://example.com/page",
                "content": "# Example\n\nFull body text.",
            }
        )

    def test_extract_maps_reader_json(self, monkeypatch):
        from plugins.web.zai.provider import ZaiWebSearchProvider

        _keyed_transport(monkeypatch, responses=[(200, "s", _rpc_result(self._reader_payload()))])
        results = ZaiWebSearchProvider().extract(["https://example.com/page"])
        assert len(results) == 1
        r = results[0]
        assert r["url"] == "https://example.com/page"
        assert r["title"] == "Example Page"
        assert r["content"].startswith("# Example")
        assert r["raw_content"] == r["content"]
        assert r["metadata"]["description"] == "A page about examples"

    def test_extract_per_url_error_isolation(self, monkeypatch):
        from plugins.web.zai.provider import ZaiWebSearchProvider

        _keyed_transport(
            monkeypatch,
            responses=[
                (200, "s", _rpc_result(self._reader_payload())),
                (500, None, "boom"),
            ],
        )
        results = ZaiWebSearchProvider().extract(["https://a.example", "https://b.example"])
        assert results[0].get("error") is None
        assert "error" in results[1]

    def test_extract_non_json_body_passthrough(self, monkeypatch):
        from plugins.web.zai.provider import ZaiWebSearchProvider

        _keyed_transport(monkeypatch, responses=[(200, "s", _rpc_result("plain text body"))])
        results = ZaiWebSearchProvider().extract(["https://example.com"])
        assert results[0]["content"] == "plain text body"

    def test_extract_without_key(self):
        from plugins.web.zai.provider import ZaiWebSearchProvider
        results = ZaiWebSearchProvider().extract(["https://example.com"])
        assert all("error" in r for r in results)


# ---------------------------------------------------------------------------
# tools.web_tools integration
# ---------------------------------------------------------------------------


class TestWebToolsIntegration:
    def test_configured_backend_accepts_zai(self, monkeypatch):
        import tools.web_tools as wt

        _keyed_transport(monkeypatch)
        monkeypatch.setattr(wt, "_load_web_config", lambda: {"backend": "zai"})
        assert wt._get_backend() == "zai"

    def test_backend_available_with_key(self, monkeypatch):
        import tools.web_tools as wt

        _keyed_transport(monkeypatch)
        assert wt._is_backend_available("zai") is True

    def test_backend_unavailable_without_key(self, monkeypatch):
        import tools.web_tools as wt

        assert wt._is_backend_available("zai") is False

    def test_zai_absent_from_auto_detect_ladder(self, monkeypatch):
        """Key presence must NOT land never-configured installs on zai —
        ZAI_API_KEY is shared with the model provider and MCP calls draw
        subscription credits."""
        import tools.web_tools as wt

        _keyed_transport(monkeypatch)
        # No config keys at all — ladder must walk past zai (picks another
        # available backend or the firecrawl default, never zai).
        monkeypatch.setattr(wt, "_load_web_config", lambda: {})
        monkeypatch.setattr(wt, "_is_tool_gateway_ready", lambda: False)
        monkeypatch.setattr(wt, "_has_env", lambda name: name == "BRAVE_SEARCH_API_KEY")
        assert wt._get_backend() == "brave-free"

    def test_search_backend_config_routing(self, monkeypatch):
        import tools.web_tools as wt

        _keyed_transport(monkeypatch)
        monkeypatch.setattr(wt, "_load_web_config", lambda: {"search_backend": "zai"})
        assert wt._get_search_backend() == "zai"

    def test_extract_backend_config_routing(self, monkeypatch):
        import tools.web_tools as wt

        _keyed_transport(monkeypatch)
        monkeypatch.setattr(wt, "_load_web_config", lambda: {"extract_backend": "zai"})
        assert wt._get_extract_backend() == "zai"
