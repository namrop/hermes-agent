"""Tests for gateway/wake.py — background wake delivery.

Two strategies:
* push-capable adapters keep the synthetic MessageEvent / handle_message path;
* the stateless API server (supports_async_delivery=False) self-POSTs
  /v1/chat/completions with the RAW session id in X-Hermes-Session-Id, so the
  wake turn resumes the REAL session instead of a parallel invisible one
  keyed by build_session_key().
"""

import asyncio
import logging
import socket

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.wake import WakeTargetNotOwned, deliver_wake, adapter_supports_push


class PushAdapter:
    """Default adapter shape — no supports_async_delivery attribute."""

    def __init__(self):
        self.handled = []

    async def handle_message(self, event):
        self.handled.append(event)


class ApiServerLikeAdapter:
    supports_async_delivery = False

    def __init__(self, host="0.0.0.0", port=0, key="test-key", model="hermes"):
        self._host = host
        self._port = port
        self._api_key = key
        self._model_name = model

    async def handle_message(self, event):  # pragma: no cover — must NOT be hit
        raise AssertionError("non-push adapter must not receive handle_message wakes")


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="group",
    )


def test_adapter_supports_push_default_true():
    assert adapter_supports_push(PushAdapter()) is True
    assert adapter_supports_push(ApiServerLikeAdapter()) is False


async def _serve(handler):
    """Spin an in-process aiohttp server on an ephemeral loopback port."""
    from aiohttp import web

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


def test_deliver_wake_non_push_self_posts_raw_session_id(monkeypatch):
    """The self-post carries the RAW session id header + bearer auth and a
    single user message with stream=false — the exact entry point real
    gateway turns use."""
    from aiohttp import web

    seen = {}

    async def handler(request):
        seen["session_id"] = request.headers.get("X-Hermes-Session-Id")
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = await request.json()
        return web.json_response({"choices": [{"message": {"content": "ok"}}]})

    async def run():
        runner, port = await _serve(handler)
        try:
            adapter = ApiServerLikeAdapter(host="0.0.0.0", port=port, key="sekrit")
            await deliver_wake(adapter, text="task done — wake", session_id="raw-sid-42")
        finally:
            await runner.cleanup()

    asyncio.run(run())
    assert seen["session_id"] == "raw-sid-42"
    assert seen["auth"] == "Bearer sekrit"
    assert seen["body"]["stream"] is False
    assert seen["body"]["messages"] == [
        {"role": "user", "content": "task done — wake"}
    ]


def test_deliver_wake_retries_429_then_succeeds(monkeypatch):
    """HTTP 429 (max_concurrent_runs cap) is transient — retried with backoff."""
    from aiohttp import web

    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "_RETRY_DELAYS_SECONDS", (0.01, 0.01, 0.01))
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return web.json_response({"error": "busy"}, status=429)
        return web.json_response({"choices": []})

    async def run():
        runner, port = await _serve(handler)
        try:
            adapter = ApiServerLikeAdapter(port=port)
            await deliver_wake(adapter, text="x", session_id="sid")
        finally:
            await runner.cleanup()

    asyncio.run(run())
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Session ownership (scar 01a0fba9, 2026-10-02): after a reboot the gateway
# self-posted leftover subagent results into three T3 (ACP) sessions and ran
# hidden turns on them. The self-post must only reach sessions the API server
# owns.
# ---------------------------------------------------------------------------


class _SessionRows:
    """The slice of SessionDB the ownership check reads."""

    def __init__(self, rows):
        self.rows = rows

    def get_session(self, session_id):
        return self.rows.get(session_id)


class OwnedApiServerAdapter(ApiServerLikeAdapter):
    """API-server stub that exposes a SessionDB, like ApiServerAdapter."""

    def __init__(self, rows, **kwargs):
        super().__init__(**kwargs)
        self._db = _SessionRows(rows)

    def _ensure_session_db(self):
        return self._db


def _counting_handler(calls):
    from aiohttp import web

    async def handler(request):
        calls.append(request.headers.get("X-Hermes-Session-Id"))
        return web.json_response({"choices": []})

    return handler


@pytest.mark.parametrize("source", ["acp", "cli", "tui", "ACP"])
def test_deliver_wake_refuses_session_driven_by_another_process(source):
    """A T3/ACP, CLI or TUI session is never self-posted into."""
    calls = []

    async def run():
        runner, port = await _serve(_counting_handler(calls))
        try:
            adapter = OwnedApiServerAdapter(
                {"t3-sess": {"id": "t3-sess", "source": source}}, port=port,
            )
            with pytest.raises(WakeTargetNotOwned) as excinfo:
                await deliver_wake(adapter, text="wake", session_id="t3-sess")
            return excinfo.value
        finally:
            await runner.cleanup()

    err = asyncio.run(run())
    assert calls == []
    assert err.session_id == "t3-sess"
    assert err.source == source.lower()


@pytest.mark.parametrize(
    "rows",
    [
        {"hq-sess": {"id": "hq-sess", "source": "api_server"}},
        # API clients may name their own source (POST /api/sessions).
        {"hq-sess": {"id": "hq-sess", "source": "lantern-reader"}},
        # No row yet: unchanged behaviour, the API server decides.
        {},
    ],
    ids=["api_server", "client-chosen-source", "no-row"],
)
def test_deliver_wake_self_posts_sessions_the_api_server_owns(rows):
    calls = []

    async def run():
        runner, port = await _serve(_counting_handler(calls))
        try:
            adapter = OwnedApiServerAdapter(rows, port=port)
            await deliver_wake(adapter, text="wake", session_id="hq-sess")
        finally:
            await runner.cleanup()

    asyncio.run(run())
    assert calls == ["hq-sess"]


def test_deliver_wake_unavailable_session_db_raises_without_posting():
    """An owner that cannot be checked is not woken; the caller retries later."""
    calls = []

    class NoDbAdapter(ApiServerLikeAdapter):
        def _ensure_session_db(self):
            return None

    async def run():
        runner, port = await _serve(_counting_handler(calls))
        try:
            with pytest.raises(RuntimeError, match="SessionDB unavailable"):
                await deliver_wake(NoDbAdapter(port=port), text="w", session_id="s")
        finally:
            await runner.cleanup()

    asyncio.run(run())
    assert calls == []


# ---------------------------------------------------------------------------
# A sent wake is never posted twice (scar 01a0fba9): a turn that outlasted the
# client timeout used to be retried, and every retry queued another full turn
# on the same session behind its turn lease.
# ---------------------------------------------------------------------------


def test_deliver_wake_slow_turn_is_not_retried(monkeypatch, caplog):
    from aiohttp import web

    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "WAKE_TURN_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(wake_mod, "_RETRY_DELAYS_SECONDS", (0.01, 0.01, 0.01))
    calls = []

    async def handler(request):
        calls.append(request.headers.get("X-Hermes-Session-Id"))
        await asyncio.sleep(1.0)  # the turn outlasts the client timeout
        return web.json_response({"choices": []})

    async def run():
        runner, port = await _serve(handler)
        try:
            adapter = ApiServerLikeAdapter(port=port)
            await deliver_wake(adapter, text="w", session_id="slow-sid")
        finally:
            await runner.cleanup()

    with caplog.at_level(logging.WARNING, logger="gateway.wake"):
        asyncio.run(run())  # returns: delivered, outcome unknown
    assert calls == ["slow-sid"]
    assert "was sent but got no complete answer" in caplog.text


def test_deliver_wake_connection_refused_is_retried_then_raises(monkeypatch, caplog):
    """Nothing reached the server, so a retry cannot start a second turn."""
    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "_RETRY_DELAYS_SECONDS", (0.01, 0.01, 0.01))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    adapter = ApiServerLikeAdapter(port=free_port)
    with caplog.at_level(logging.WARNING, logger="gateway.wake"):
        with pytest.raises(RuntimeError, match="gave up .* after 4 attempts"):
            asyncio.run(deliver_wake(adapter, text="w", session_id="sid"))
    assert caplog.text.count("could not connect") == 4
