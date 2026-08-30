"""Focused tests for API server session-control endpoints."""

import asyncio
import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB


@pytest.fixture
def session_db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        yield db
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            close()


@pytest.fixture
def adapter(session_db):
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._session_db = session_db
    return adapter


@pytest.fixture
def auth_adapter(session_db):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sk-test"}))
    adapter._session_db = session_db
    return adapter


def _create_session_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_get("/v1/capabilities", adapter._handle_capabilities)
    app.router.add_get("/api/sessions", adapter._handle_list_sessions)
    app.router.add_post("/api/sessions", adapter._handle_create_session)
    app.router.add_get("/api/sessions/{session_id}", adapter._handle_get_session)
    app.router.add_patch("/api/sessions/{session_id}", adapter._handle_patch_session)
    app.router.add_delete("/api/sessions/{session_id}", adapter._handle_delete_session)
    app.router.add_get("/api/sessions/{session_id}/messages", adapter._handle_session_messages)
    app.router.add_get("/api/sessions/{session_id}/system_prompt", adapter._handle_session_system_prompt)
    app.router.add_get("/api/sessions/{session_id}/approvals", adapter._handle_session_approvals)
    app.router.add_post("/api/sessions/{session_id}/approval", adapter._handle_session_approval)
    app.router.add_post("/api/sessions/{session_id}/stop", adapter._handle_session_stop)
    app.router.add_get("/api/approvals", adapter._handle_list_approvals)
    app.router.add_post("/api/sessions/{session_id}/fork", adapter._handle_fork_session)
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    app.router.add_post("/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream)
    return app


@pytest.mark.asyncio
async def test_capabilities_advertises_session_control_surface(adapter):
    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/v1/capabilities")
        assert resp.status == 200
        data = await resp.json()

    features = data["features"]
    assert features["session_resources"] is True
    assert features["session_chat"] is True
    assert features["session_chat_streaming"] is True
    assert features["session_fork"] is True
    assert features["run_steer"] is True
    assert features["admin_config_rw"] is False
    assert features["memory_write_api"] is False
    assert features["skills_api"] is True
    assert features["realtime_voice"] is False
    assert data["endpoints"]["sessions"] == {"method": "GET", "path": "/api/sessions"}
    assert data["endpoints"]["session_chat_stream"] == {
        "method": "POST",
        "path": "/api/sessions/{session_id}/chat/stream",
    }
    assert data["endpoints"]["run_steer"] == {
        "method": "POST",
        "path": "/v1/runs/{run_id}/steer",
    }


@pytest.mark.asyncio
async def test_session_messages_default_to_latest_bounded_page(adapter, session_db):
    session_id = session_db.create_session("bounded-messages", "api_server")
    session_db.replace_messages(
        session_id,
        [{"role": "user", "content": f"msg {i}"} for i in range(501)],
    )

    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get(f"/api/sessions/{session_id}/messages")
        assert resp.status == 200
        payload = await resp.json()

        explicit_resp = await cli.get(
            f"/api/sessions/{session_id}/messages?limit=2&offset=1"
        )
        assert explicit_resp.status == 200
        explicit = await explicit_resp.json()

    assert payload["pagination"] == {
        "limit": 500,
        "offset": 0,
        "order": "latest",
        "returned": 500,
    }
    assert payload["data"][0]["content"] == "msg 1"
    assert payload["data"][-1]["content"] == "msg 500"
    assert [message["content"] for message in explicit["data"]] == [
        "msg 1",
        "msg 2",
    ]


@pytest.mark.asyncio
async def test_run_agent_binds_api_session_context_for_tool_env(adapter, monkeypatch):
    """API-server request sessions should reach tools and terminal subprocess env."""
    monkeypatch.setenv("HERMES_SESSION_ID", "stale-session")
    observed = {}

    class FakeAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0

        def __init__(self, session_id: str):
            self.session_id = session_id

        def run_conversation(self, user_message, conversation_history, task_id):
            from gateway.session_context import get_session_env
            from tools.environments.local import _make_run_env

            observed["task_id"] = task_id
            observed["context_session_id"] = get_session_env("HERMES_SESSION_ID")
            observed["context_platform"] = get_session_env("HERMES_SESSION_PLATFORM")
            observed["context_session_key"] = get_session_env("HERMES_SESSION_KEY")
            observed["child_session_id"] = _make_run_env({}).get("HERMES_SESSION_ID")
            return {"final_response": "ok"}

    def fake_create_agent(**kwargs):
        return FakeAgent(kwargs["session_id"])

    monkeypatch.setattr(adapter, "_create_agent", fake_create_agent)

    result, usage = await adapter._run_agent(
        user_message="hello",
        conversation_history=[],
        session_id="request-session",
        gateway_session_key="request-key",
    )

    assert result["session_id"] == "request-session"
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["total_tokens"] == 0
    assert "runtime" not in usage
    assert observed == {
        "task_id": "request-session",
        "context_session_id": "request-session",
        "context_platform": "api_server",
        "context_session_key": "request-key",
        "child_session_id": "request-session",
    }


@pytest.mark.asyncio
async def test_run_agent_registers_active_run_id_for_steering(adapter, monkeypatch):
    observed = {}

    class FakeAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0

        def __init__(self, session_id: str):
            self.session_id = session_id

        def steer(self, text: str) -> bool:
            observed["steer_text"] = text
            return True

        def run_conversation(self, user_message, conversation_history, task_id):
            observed["registered"] = adapter._active_run_agents.get("run_steer_test") is self
            observed["task_id"] = task_id
            return {"final_response": "ok"}

    def fake_create_agent(**kwargs):
        return FakeAgent(kwargs["session_id"])

    monkeypatch.setattr(adapter, "_create_agent", fake_create_agent)

    result, usage = await adapter._run_agent(
        user_message="hello",
        conversation_history=[],
        session_id="request-session",
        active_run_id="run_steer_test",
    )

    assert result["session_id"] == "request-session"
    assert usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    assert observed == {"registered": True, "task_id": "request-session"}
    assert "run_steer_test" not in adapter._active_run_agents


@pytest.mark.asyncio
async def test_session_chat_stream_disconnect_keeps_control_refs_until_executor_finishes(
    adapter, session_db
):
    """Disconnects must interrupt the live run without dropping its control refs early."""
    session_id = session_db.create_session("disconnect-stream-session", "api_server")
    run_started = threading.Event()
    interrupt_called = threading.Event()
    allow_finish = threading.Event()
    write_calls = {"count": 0}

    class FakeAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0

        def __init__(self, stream_delta_callback):
            self._stream_delta_callback = stream_delta_callback
            self.session_id = session_id

        def interrupt(self, _message=None):
            interrupt_called.set()

        def run_conversation(self, user_message, conversation_history, task_id):
            del user_message, conversation_history, task_id
            run_started.set()
            self._stream_delta_callback("hello")
            allow_finish.wait(timeout=5)
            return {"final_response": "done", "session_id": session_id}

    class DisconnectingStreamResponse:
        async def prepare(self, request):
            del request

        async def write(self, payload):
            del payload
            write_calls["count"] += 1
            if write_calls["count"] >= 3:
                raise ConnectionResetError("simulated client disconnect")

    request = MagicMock()
    request.headers = {}
    request.match_info = {"session_id": session_id}

    def _create_agent(**kwargs):
        return FakeAgent(kwargs["stream_delta_callback"])

    with patch.object(
        adapter,
        "_get_existing_session_or_404",
        return_value=({"id": session_id}, None),
    ), patch.object(
        adapter,
        "_read_json_body",
        return_value=({"message": "stream please"}, None),
    ), patch.object(
        adapter,
        "_create_agent",
        side_effect=_create_agent,
    ), patch(
        "gateway.platforms.api_server.web.StreamResponse",
        return_value=DisconnectingStreamResponse(),
    ):
        handler_task = asyncio.create_task(adapter._handle_session_chat_stream(request))

        for _ in range(60):
            if run_started.is_set():
                break
            await asyncio.sleep(0.05)

        assert run_started.is_set()
        run_id = next(iter(adapter._run_statuses))

        for _ in range(40):
            if interrupt_called.is_set():
                break
            await asyncio.sleep(0.05)

        assert interrupt_called.is_set()
        assert run_id in adapter._active_run_agents
        # Not in _active_run_tasks: session-stream turns are counted via
        # _inflight_agent_runs; a task entry would double-count them in the
        # shutdown drain (active_agent_work_count).
        assert run_id not in adapter._active_run_tasks
        assert not handler_task.done()

        allow_finish.set()
        await handler_task

    assert run_id not in adapter._active_run_agents


@pytest.mark.asyncio
async def test_session_chat_stream_run_completed_carries_turn_transcript(adapter, session_db):
    """run.completed must include the full interleaved turn transcript so a
    client that lost intermediate (pre-tool-call) assistant text from the live
    delta stream can reconcile without a separate /messages fetch. Refs #34703.
    """
    import json as _json

    session_id = session_db.create_session("transcript-session", "api_server")

    async def fake_run(**kwargs):
        # Stream the intermediate planning text the way a real turn would.
        kwargs["stream_delta_callback"]("Let me search for that:")
        kwargs["stream_delta_callback"]("Here is the summary.")
        result = {
            "final_response": "Here is the summary.",
            "session_id": session_id,
            "messages": [
                {"role": "user", "content": "search then summarize"},
                {
                    "role": "assistant",
                    "content": "Let me search for that:",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "web_search", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "content": "results", "tool_call_id": "call_1", "tool_name": "web_search"},
                {"role": "assistant", "content": "Here is the summary."},
            ],
        }
        return result, {"total_tokens": 6}

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat/stream",
                json={"message": "search then summarize"},
            )
            assert resp.status == 200
            body = await resp.text()

    # Pull the run.completed event payload out of the SSE body.
    run_completed_payload = None
    for block in body.split("\n\n"):
        if "event: run.completed" in block:
            for line in block.splitlines():
                if line.startswith("data: "):
                    run_completed_payload = _json.loads(line[len("data: "):])
            break
    assert run_completed_payload is not None, body
    messages = run_completed_payload.get("messages")
    assert isinstance(messages, list) and messages, run_completed_payload

    # The colon-ended intermediate text that preceded the tool call must be present.
    contents = [m.get("content") for m in messages]
    assert "Let me search for that:" in contents
    assert "Here is the summary." in contents
    # No prior-turn user message should leak into the per-turn slice.
    assert all(m.get("role") in ("assistant", "tool") for m in messages)
    # The tool call is preserved alongside the intermediate text.
    assert any(m.get("tool_calls") for m in messages)


# ---------------------------------------------------------------------------
# Session-persisted model threading + provider-auth failure surfacing
# (salvaged from PR #57947 by @FvanW and PR #59941 by @kaishi00)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_chat_resolves_stored_model_route_alias(session_db, monkeypatch):
    """A session-persisted model that matches a model_routes alias must go
    through the route path (so route provider/credentials apply) and NOT be
    passed as a raw session_model (idea from PR #59941 by @kaishi00)."""
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={"model_routes": {"alias": {"model": "route/model", "provider": "openrouter"}}},
        )
    )
    adapter._session_db = session_db
    session_id = session_db.create_session("route-pinned-session", "api_server", model="alias")

    mock_run = AsyncMock(return_value=({"final_response": "ok", "session_id": session_id}, {"total_tokens": 1}))
    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", mock_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={"message": "hi"},
            )
            assert resp.status == 200

    _, kwargs = mock_run.call_args
    assert kwargs["route"] == {"model": "route/model", "provider": "openrouter"}
    assert kwargs["session_model"] is None


@pytest.mark.asyncio
async def test_session_chat_treats_pre_existing_poisoned_row_as_no_model(session_db):
    """A session row created before the alias-leak fix may still have the
    virtual model alias (e.g. "hermes-agent") persisted literally as its
    model. Reading that back must NOT thread it through as a raw
    session_model override — it must fall through to the global default,
    exactly like a row that never had a model at all (#session-model-
    alias-leak)."""
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._session_db = session_db
    session_id = session_db.create_session(
        "poisoned-session", "api_server", model=adapter._model_name
    )

    mock_run = AsyncMock(return_value=({"final_response": "ok", "session_id": session_id}, {"total_tokens": 1}))
    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", mock_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={"message": "hi"},
            )
            assert resp.status == 200

    _, kwargs = mock_run.call_args
    assert kwargs["session_model"] is None


@pytest.mark.asyncio
async def test_session_chat_stream_treats_pre_existing_poisoned_row_as_no_model(session_db):
    """Streaming twin of the above: the SSE chat path must apply the same
    guard against a pre-existing poisoned session row."""
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._session_db = session_db
    session_id = session_db.create_session(
        "poisoned-stream-session", "api_server", model=adapter._model_name
    )

    async def fake_run(**kwargs):
        return {"final_response": "ok", "session_id": session_id}, {"total_tokens": 1}

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run) as mock_run:
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat/stream",
                json={"message": "hi"},
            )
            assert resp.status == 200
            # Drain the SSE body: the 200 lands before the streaming task
            # invokes _run_agent, so asserting on call_args without reading
            # the body races the handler (flaked on loaded CI runners).
            await resp.text()

    _, kwargs = mock_run.call_args
    assert kwargs["session_model"] is None


def _register_session_model_route(app, adapter):
    app.router.add_post("/api/sessions/{session_id}/model", adapter._handle_session_model_lock)


def _patch_api_server_runtime(monkeypatch):
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_key": "sk-global",
            "base_url": "https://openrouter.example/v1",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "global/model")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})
    monkeypatch.setattr(
        "gateway.run.GatewayRunner._load_reasoning_config",
        staticmethod(lambda model="": {}),
    )
    monkeypatch.setattr(
        "gateway.run.GatewayRunner._load_fallback_model",
        staticmethod(lambda: None),
    )
    monkeypatch.setattr("gateway.run._current_max_iterations", lambda: 90)
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *_: set())
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs_for_provider",
        lambda provider: {
            "provider": provider,
            "api_key": f"sk-{provider}",
            "base_url": f"https://{provider}.example/v1",
            "api_mode": "chat_completions",
        },
    )


@pytest.mark.asyncio
async def test_create_session_respects_browser_source_and_model_lock(adapter, session_db):
    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/sessions",
            json={
                "id": "browser-lock-session",
                "source": "hermes_browser",
                "provider": "nous",
                "model": "x-ai/grok-4.5",
                "require_model_lock": True,
                "title": "Browser lock",
                "system_prompt": "browser prompt",
            },
        )
        assert resp.status == 201, await resp.text()
        payload = await resp.json()

    assert payload["session"]["source"] == "hermes_browser"
    assert payload["session"]["model"] == "x-ai/grok-4.5"
    row = session_db.get_session("browser-lock-session")
    assert row["source"] == "hermes_browser"
    assert row["model"] == "x-ai/grok-4.5"
    import json as _json
    model_config = row.get("model_config")
    if isinstance(model_config, str):
        model_config = _json.loads(model_config)
    assert model_config["browser_model_lock"]["provider"] == "nous"
    assert model_config["browser_model_lock"]["model"] == "x-ai/grok-4.5"
    assert model_config["browser_model_lock"]["confirmed"] is True


@pytest.mark.asyncio
async def test_session_model_lock_endpoint_then_chat_reuses_persisted_lock_and_provider_credentials(
    adapter,
    session_db,
    monkeypatch,
):
    session_id = session_db.create_session(
        "endpoint-lock-chat",
        "api_server",
        model="gpt-5.5",
        system_prompt="Conversation started:\nModel: gpt-5.5\nProvider: openai-codex\n",
    )
    captured = {}

    class FakeAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0

        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.session_id = kwargs["session_id"]
            self.provider = kwargs.get("provider") or ""
            self.model = kwargs.get("model") or ""

        def run_conversation(self, user_message, conversation_history, task_id):
            return {"final_response": "locked", "session_id": self.session_id}

    _patch_api_server_runtime(monkeypatch)
    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
    monkeypatch.setattr(
        adapter,
        "_session_model_override_for",
        lambda *_: {
            "model": "session/override-model",
            "provider": "openai-codex",
            "api_key": "sk-session-override",
            "base_url": "https://override.example/v1",
            "api_mode": "codex_responses",
        },
    )

    app = _create_session_app(adapter)
    _register_session_model_route(app, adapter)
    with patch.object(adapter, "_resolve_route", return_value=None):
        async with TestClient(TestServer(app)) as cli:
            lock_resp = await cli.post(
                f"/api/sessions/{session_id}/model",
                json={
                    "provider": "nous",
                    "model": "x-ai/grok-4.5",
                    "require_model_lock": True,
                },
            )
            assert lock_resp.status == 200, await lock_resp.text()

            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={"message": "use the stored lock"},
            )
            assert resp.status == 200, await resp.text()
            payload = await resp.json()

    assert captured["provider"] == "nous"
    assert captured["model"] == "x-ai/grok-4.5"
    assert captured["api_key"] == "sk-nous"
    assert captured["base_url"] == "https://nous.example/v1"
    assert payload["runtime"]["provider"] == "nous"
    assert payload["runtime"]["model"] == "x-ai/grok-4.5"
    assert payload["runtime"]["requested"] == {
        "provider": "nous",
        "model": "x-ai/grok-4.5",
    }
    assert payload["runtime"]["route_source"] == "session_model_lock"


@pytest.mark.asyncio
async def test_session_model_lock_endpoint_then_chat_stream_reuses_persisted_lock(
    adapter,
    session_db,
):
    session_id = session_db.create_session("endpoint-lock-stream", "api_server")
    captured = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        kwargs["stream_delta_callback"]("hi")
        return (
            {
                "final_response": "hi",
                "session_id": session_id,
                "runtime": {
                    "provider": "nous",
                    "model": "x-ai/grok-4.5",
                    "requested": {"provider": "nous", "model": "x-ai/grok-4.5"},
                    "route_source": "session_model_lock",
                },
            },
            {
                "total_tokens": 1,
                "runtime": {
                    "provider": "nous",
                    "model": "x-ai/grok-4.5",
                    "requested": {"provider": "nous", "model": "x-ai/grok-4.5"},
                    "route_source": "session_model_lock",
                },
            },
        )

    app = _create_session_app(adapter)
    _register_session_model_route(app, adapter)
    with patch.object(adapter, "_resolve_route", return_value=None), patch.object(
        adapter,
        "_run_agent",
        side_effect=fake_run,
    ):
        async with TestClient(TestServer(app)) as cli:
            lock_resp = await cli.post(
                f"/api/sessions/{session_id}/model",
                json={
                    "provider": "nous",
                    "model": "x-ai/grok-4.5",
                    "require_model_lock": True,
                },
            )
            assert lock_resp.status == 200, await lock_resp.text()

            resp = await cli.post(
                f"/api/sessions/{session_id}/chat/stream",
                json={"message": "stream with stored lock"},
            )
            assert resp.status == 200, await resp.text()
            body = await resp.text()

    assert captured["route"] == {"provider": "nous", "model": "x-ai/grok-4.5"}
    assert captured["requested_runtime"]["provider"] == "nous"
    assert captured["requested_runtime"]["model"] == "x-ai/grok-4.5"
    assert captured["route_source"] == "session_model_lock"
    assert "x-ai/grok-4.5" in body


@pytest.mark.asyncio
async def test_run_agent_reports_actual_agent_runtime_not_requested_metadata(adapter, monkeypatch):
    class FakeAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0

        def __init__(self):
            self.session_id = "runtime-session"
            self.provider = "actual-provider"
            self.model = "actual-model"
            self._hermes_api_runtime = {
                "provider": "requested-provider",
                "model": "requested-model",
                "route_source": "raw_request",
            }

        def run_conversation(self, user_message, conversation_history, task_id):
            return {"final_response": "ok", "session_id": self.session_id}

    monkeypatch.setattr(adapter, "_create_agent", lambda **kwargs: FakeAgent())

    result, usage = await adapter._run_agent(
        user_message="hello",
        conversation_history=[],
        session_id="runtime-session",
        route={"provider": "requested-provider", "model": "requested-model"},
        requested_runtime={
            "provider": "requested-provider",
            "model": "requested-model",
        },
        route_source="session_model_lock",
    )

    assert result["runtime"]["provider"] == "actual-provider"
    assert result["runtime"]["model"] == "actual-model"
    assert result["runtime"]["requested"] == {
        "provider": "requested-provider",
        "model": "requested-model",
    }
    assert usage["runtime"]["provider"] == "actual-provider"
    assert usage["runtime"]["model"] == "actual-model"


@pytest.mark.asyncio
async def test_confirmed_runtime_lock_rejects_actual_runtime_mismatch(adapter, monkeypatch):
    class FakeAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0
        session_id = "mismatch-session"
        provider = "fallback-provider"
        model = "fallback-model"

        def run_conversation(self, user_message, conversation_history, task_id):
            return {"final_response": "wrong runtime", "session_id": self.session_id}

    monkeypatch.setattr(adapter, "_create_agent", lambda **kwargs: FakeAgent())

    with pytest.raises(RuntimeError, match="confirmed model lock runtime mismatch"):
        await adapter._run_agent(
            user_message="hello",
            conversation_history=[],
            session_id="mismatch-session",
            route={"provider": "nous", "model": "x-ai/grok-4.5"},
            requested_runtime={"provider": "nous", "model": "x-ai/grok-4.5"},
            route_source="session_model_lock",
            confirmed_runtime_lock=True,
        )


def test_confirmed_runtime_lock_disables_global_fallback_model(adapter, monkeypatch):
    _patch_api_server_runtime(monkeypatch)
    monkeypatch.setattr(
        "gateway.run.GatewayRunner._load_fallback_model",
        staticmethod(lambda: "openrouter/fallback-model"),
    )
    captured = {}

    class FakeAgent:
        provider = "nous"
        model = "x-ai/grok-4.5"

        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)

    adapter._create_agent(
        session_id="locked-session",
        route={"provider": "nous", "model": "x-ai/grok-4.5"},
        confirmed_runtime_lock=True,
    )

    assert captured["fallback_model"] is None


@pytest.mark.asyncio
async def test_unconfirmed_request_does_not_replace_confirmed_session_lock(adapter, session_db):
    session_id = session_db.create_session("one-off-override", "api_server")
    session_db.update_session_runtime_lock(
        session_id,
        provider="nous",
        model="x-ai/grok-4.5",
        route_source="raw_request",
        confirmed=True,
    )
    mock_run = AsyncMock(
        return_value=(
            {
                "final_response": "ok",
                "session_id": session_id,
                "runtime": {"provider": "openrouter", "model": "anthropic/claude-sonnet"},
            },
            {"total_tokens": 1},
        )
    )
    app = _create_session_app(adapter)
    with patch.object(adapter, "_resolve_route", return_value=None), patch.object(
        adapter,
        "_run_agent",
        mock_run,
    ):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={
                    "message": "one turn only",
                    "provider": "openrouter",
                    "model": "anthropic/claude-sonnet",
                },
            )
            assert resp.status == 200, await resp.text()

    import json as _json

    row = session_db.get_session(session_id)
    config = row["model_config"]
    if isinstance(config, str):
        config = _json.loads(config)
    assert config["browser_model_lock"]["provider"] == "nous"
    assert config["browser_model_lock"]["model"] == "x-ai/grok-4.5"
    assert config["browser_model_lock"]["confirmed"] is True


@pytest.mark.asyncio
async def test_require_model_lock_hard_fails_when_global_default_would_be_used(adapter, session_db, monkeypatch):
    session_id = session_db.create_session("lock-fail-session", "api_server")
    monkeypatch.setattr(adapter, "_model_name", "gpt-5.5")
    app = _create_session_app(adapter)
    with patch.object(adapter, "_resolve_route", return_value=None), patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
        async with TestClient(TestServer(app)) as cli:
            # empty model + require_model_lock must not silently fall through
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={
                    "message": "hello",
                    "provider": "nous",
                    "model": "",
                    "require_model_lock": True,
                },
            )
            assert resp.status in (400, 409), await resp.text()
            body = await resp.json()
            assert body["error"]["code"] in {"model_lock_unavailable", "invalid_model_lock", "missing_model"}
    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_patch_session_persists_pinned_and_archived(adapter, session_db):
    """PATCH must accept the durable pin/archive flags and round-trip them.

    These were rejected as unsupported fields, so every pin the desktop made
    400'd silently (the client swallows the error) and the pin only ever lived
    in that one app's localStorage. The auto-archive sweep reads
    `sessions.pinned` server-side, so an unpersisted pin does not protect the
    chat it was supposed to keep.
    """
    session_id = session_db.create_session("pin-session", "api_server")
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.patch(f"/api/sessions/{session_id}", json={"pinned": True})
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["session"]["pinned"] is True

        # The flag is durable, not just echoed back from the request body.
        assert bool(session_db.get_session(session_id)["pinned"]) is True

        resp = await cli.get(f"/api/sessions/{session_id}")
        assert (await resp.json())["session"]["pinned"] is True

        resp = await cli.patch(f"/api/sessions/{session_id}", json={"pinned": False})
        assert (await resp.json())["session"]["pinned"] is False
        assert bool(session_db.get_session(session_id)["pinned"]) is False

        resp = await cli.patch(f"/api/sessions/{session_id}", json={"archived": True})
        assert (await resp.json())["session"]["archived"] is True
        assert bool(session_db.get_session(session_id)["archived"]) is True


@pytest.mark.asyncio
async def test_patch_session_rejects_non_boolean_pinned(adapter, session_db):
    session_id = session_db.create_session("pin-type-session", "api_server")
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.patch(f"/api/sessions/{session_id}", json={"pinned": "yes"})
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["error"]["code"] == "invalid_session_field"


@pytest.mark.asyncio
async def test_patch_session_still_rejects_unknown_fields(adapter, session_db):
    session_id = session_db.create_session("unknown-field-session", "api_server")
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.patch(f"/api/sessions/{session_id}", json={"nonsense": 1})
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["error"]["code"] == "unsupported_session_field"


@pytest.mark.asyncio
async def test_session_system_prompt_returns_stored_text_and_hash(adapter, session_db):
    """GET /api/sessions/{id}/system_prompt resolves the content-addressed prompt.

    The generic session payload deliberately carries only has_system_prompt;
    this sub-resource is the sanctioned full-text read (Vikunja #606), and the
    hash must match the dedup store's sha256 addressing.
    """
    import hashlib

    prompt = "You are Hermes. Keep the coherence well coherent."
    session_id = session_db.create_session(
        "sysprompt-session", "api_server", system_prompt=prompt
    )
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get(f"/api/sessions/{session_id}/system_prompt")
        assert resp.status == 200, await resp.text()
        payload = await resp.json()

    assert payload["object"] == "hermes.session.system_prompt"
    assert payload["session_id"] == session_id
    assert payload["system_prompt"] == prompt
    assert payload["system_prompt_hash"] == hashlib.sha256(
        prompt.encode("utf-8")
    ).hexdigest()

    # The generic session payload still exposes only the boolean.
    async with TestClient(TestServer(_create_session_app(adapter))) as cli:
        resp = await cli.get(f"/api/sessions/{session_id}")
        session_payload = (await resp.json())["session"]
    assert session_payload["has_system_prompt"] is True
    assert "system_prompt" not in session_payload


@pytest.mark.asyncio
async def test_session_system_prompt_null_when_absent(adapter, session_db):
    session_id = session_db.create_session("no-sysprompt-session", "api_server")
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get(f"/api/sessions/{session_id}/system_prompt")
        assert resp.status == 200, await resp.text()
        payload = await resp.json()

    assert payload["session_id"] == session_id
    assert payload["system_prompt"] is None
    assert payload["system_prompt_hash"] is None


@pytest.mark.asyncio
async def test_session_system_prompt_unknown_session_404s(adapter):
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/api/sessions/no-such-session/system_prompt")
        assert resp.status == 404, await resp.text()
        assert (await resp.json())["error"]["code"] == "session_not_found"


# ---------------------------------------------------------------------------
# Session approval + stop surface (Vikunja #613)
# ---------------------------------------------------------------------------


@pytest.fixture
def approval_queues():
    """Give each test an empty gateway approval queue and restore it after.

    ``tools.approval._gateway_queues`` is process-global (it is reached from
    agent threads that have no handle on anything else), so a test that leaves
    an entry behind changes what the next test's global listing returns.
    """
    from tools import approval as approval_mod

    saved = dict(approval_mod._gateway_queues)
    approval_mod._gateway_queues.clear()
    try:
        yield approval_mod._gateway_queues
    finally:
        approval_mod._gateway_queues.clear()
        approval_mod._gateway_queues.update(saved)


def _enqueue_approval(queues, session_key: str, **data):
    """Append a pending approval entry to *session_key*'s queue."""
    from tools.approval import _ApprovalEntry

    payload = {
        "command": "rm -rf /tmp/demo",
        "description": "recursive delete",
        "pattern_key": "rm_rf",
        "pattern_keys": ["rm_rf"],
        "allow_permanent": True,
        "allow_session": True,
    }
    payload.update(data)
    entry = _ApprovalEntry(payload)
    queues.setdefault(session_key, []).append(entry)
    return entry


class _StubTurn:
    def __init__(self, agent):
        self.agent = agent


class _StubSessionState:
    def __init__(self, agent):
        self.turn = _StubTurn(agent)


class _StubRunner:
    """Minimal gateway-runner stand-in exposing only _peek_session_state."""

    def __init__(self, states=None):
        self._states = states or {}

    def _peek_session_state(self, session_key):
        return self._states.get(session_key)


class _StubAgent:
    def __init__(self):
        self.interrupts = []

    def hard_interrupt(self, message=None):
        self.interrupts.append(message)
        return True


@pytest.mark.asyncio
async def test_capabilities_advertises_approval_and_stop_surface(adapter):
    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/v1/capabilities")
        assert resp.status == 200
        data = await resp.json()

    features = data["features"]
    assert features["session_approvals"] is True
    assert features["session_approval"] is True
    assert features["session_stop"] is True
    assert features["approvals_list"] is True
    endpoints = data["endpoints"]
    assert endpoints["session_approvals"] == {
        "method": "GET",
        "path": "/api/sessions/{session_id}/approvals",
    }
    assert endpoints["session_approval"] == {
        "method": "POST",
        "path": "/api/sessions/{session_id}/approval",
    }
    assert endpoints["session_stop"] == {
        "method": "POST",
        "path": "/api/sessions/{session_id}/stop",
    }
    assert endpoints["approvals_list"] == {"method": "GET", "path": "/api/approvals"}


@pytest.mark.asyncio
async def test_session_approvals_lists_pending_without_session_key(
    adapter, session_db, approval_queues
):
    """The pending list carries enough to adjudicate — and never the key.

    ``session_key`` resolves approvals for the whole conversation, so echoing
    it would hand every reader the capability the endpoint exists to mediate.
    """
    session_id = session_db.create_session(
        "approval-session", "discord", session_key="discord:chan-1"
    )
    entry = _enqueue_approval(
        approval_queues,
        "discord:chan-1",
        turn_id="turn-9",
        tool_call_id="call-3",
    )
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get(f"/api/sessions/{session_id}/approvals")
        assert resp.status == 200, await resp.text()
        payload = await resp.json()

    assert payload["object"] == "hermes.session.approvals"
    assert payload["session_id"] == session_id
    assert len(payload["approvals"]) == 1
    item = payload["approvals"][0]
    assert item["request_id"] == entry.data["request_id"]
    assert item["command"] == "rm -rf /tmp/demo"
    assert item["pattern_keys"] == ["rm_rf"]
    assert item["choices"] == ["once", "session", "always", "deny"]
    assert item["turn_id"] == "turn-9"
    assert item["tool_call_id"] == "call-3"
    # F1 enrichment: both stamps are present and ordered.
    assert item["expires_at"] > item["created_at"]
    assert "session_key" not in item
    assert "discord:chan-1" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_session_approvals_smart_denied_narrows_choices(
    adapter, session_db, approval_queues
):
    session_id = session_db.create_session(
        "smart-denied-session", "discord", session_key="discord:chan-sd"
    )
    _enqueue_approval(
        approval_queues,
        "discord:chan-sd",
        smart_denied=True,
        allow_permanent=False,
        allow_session=False,
    )
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        payload = await (await cli.get(f"/api/sessions/{session_id}/approvals")).json()

    item = payload["approvals"][0]
    assert item["smart_denied"] is True
    assert item["allow_permanent"] is False
    assert item["allow_session"] is False
    assert item["choices"] == ["once", "deny"]


@pytest.mark.asyncio
async def test_session_approvals_empty_without_gateway_key(adapter, session_db, approval_queues):
    """An API-created session has no routing key, so it has no queue."""
    session_id = session_db.create_session("keyless-session", "api_server")
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get(f"/api/sessions/{session_id}/approvals")
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["approvals"] == []


@pytest.mark.asyncio
async def test_session_approvals_unknown_session_404s(adapter):
    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/api/sessions/no-such-session/approvals")
        assert resp.status == 404, await resp.text()
        assert (await resp.json())["error"]["code"] == "session_not_found"


@pytest.mark.asyncio
async def test_session_approval_requires_auth(auth_adapter, session_db, approval_queues):
    session_id = session_db.create_session(
        "auth-approval-session", "discord", session_key="discord:chan-auth"
    )
    _enqueue_approval(approval_queues, "discord:chan-auth")
    app = _create_session_app(auth_adapter)

    async with TestClient(TestServer(app)) as cli:
        assert (await cli.get(f"/api/sessions/{session_id}/approvals")).status == 401
        assert (
            await cli.post(
                f"/api/sessions/{session_id}/approval", json={"choice": "once"}
            )
        ).status == 401
        assert (await cli.post(f"/api/sessions/{session_id}/stop", json={})).status == 401
        assert (await cli.get("/api/approvals")).status == 401

    # The rejected calls must not have resolved anything.
    assert len(approval_queues["discord:chan-auth"]) == 1


@pytest.mark.asyncio
async def test_session_approval_resolves_by_request_id(adapter, session_db, approval_queues):
    session_id = session_db.create_session(
        "resolve-session", "discord", session_key="discord:chan-2"
    )
    first = _enqueue_approval(approval_queues, "discord:chan-2", command="cmd-1")
    second = _enqueue_approval(approval_queues, "discord:chan-2", command="cmd-2")
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            f"/api/sessions/{session_id}/approval",
            json={"choice": "approve", "request_id": second.data["request_id"]},
        )
        assert resp.status == 200, await resp.text()
        payload = await resp.json()

    assert payload == {
        "object": "hermes.session.approval_response",
        "session_id": session_id,
        "request_id": second.data["request_id"],
        "choice": "once",
        "resolved": 1,
    }
    # The targeted entry was resolved; the FIFO head was left alone.
    assert second.result == "once"
    assert second.event.is_set()
    assert first.result is None
    assert [e.data["request_id"] for e in approval_queues["discord:chan-2"]] == [
        first.data["request_id"]
    ]


@pytest.mark.asyncio
async def test_session_approval_requires_request_id_when_multiple_pending(
    adapter, session_db, approval_queues
):
    """session_key is per-conversation: a bare FIFO could answer another session.

    Two sessions can share one routing key, so an untargeted resolve is not
    merely ambiguous — it can consent on behalf of a session the caller never
    named. Refuse rather than guess.
    """
    session_id = session_db.create_session(
        "ambiguous-session", "discord", session_key="discord:chan-3"
    )
    _enqueue_approval(approval_queues, "discord:chan-3", command="cmd-1")
    _enqueue_approval(approval_queues, "discord:chan-3", command="cmd-2")
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            f"/api/sessions/{session_id}/approval", json={"choice": "deny"}
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["error"]["code"] == "approval_request_id_required"

        # all=true is the sanctioned untargeted form and still works.
        resp = await cli.post(
            f"/api/sessions/{session_id}/approval",
            json={"choice": "deny", "all": True, "reason": "not now"},
        )
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["resolved"] == 2

    assert "discord:chan-3" not in approval_queues


@pytest.mark.asyncio
async def test_session_approval_single_pending_needs_no_request_id(
    adapter, session_db, approval_queues
):
    session_id = session_db.create_session(
        "single-pending-session", "discord", session_key="discord:chan-4"
    )
    entry = _enqueue_approval(approval_queues, "discord:chan-4")
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            f"/api/sessions/{session_id}/approval",
            json={"choice": "deny", "reason": "too broad"},
        )
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["resolved"] == 1

    assert entry.result == "deny"
    assert entry.reason == "too broad"


@pytest.mark.asyncio
async def test_session_approval_no_pending_returns_409(adapter, session_db, approval_queues):
    session_id = session_db.create_session(
        "nothing-pending-session", "discord", session_key="discord:chan-5"
    )
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            f"/api/sessions/{session_id}/approval", json={"choice": "once"}
        )
        assert resp.status == 409, await resp.text()
        assert (await resp.json())["error"]["code"] == "approval_not_pending"


@pytest.mark.asyncio
async def test_session_approval_rejects_unknown_choice(adapter, session_db, approval_queues):
    session_id = session_db.create_session(
        "bad-choice-session", "discord", session_key="discord:chan-6"
    )
    entry = _enqueue_approval(approval_queues, "discord:chan-6")
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            f"/api/sessions/{session_id}/approval", json={"choice": "maybe"}
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["error"]["code"] == "invalid_approval_choice"

    assert entry.result is None


@pytest.mark.asyncio
async def test_session_approval_keyless_session_409s(adapter, session_db, approval_queues):
    session_id = session_db.create_session("keyless-approval-session", "api_server")
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            f"/api/sessions/{session_id}/approval", json={"choice": "once"}
        )
        assert resp.status == 409, await resp.text()
        assert (await resp.json())["error"]["code"] == "approval_not_active"


@pytest.mark.asyncio
async def test_session_approval_first_response_wins_loser_gets_409(
    adapter, session_db, approval_queues
):
    """THE RACE: two clients answer the same approval; exactly one resolves it.

    Diadem's whole point is that a human and an agent may be looking at the
    same pending approval. The queue resolves under a lock, so the second
    caller must be told the state moved (409) rather than silently succeeding
    on an approval it did not actually decide.
    """
    session_id = session_db.create_session(
        "race-session", "discord", session_key="discord:chan-7"
    )
    entry = _enqueue_approval(approval_queues, "discord:chan-7")
    app = _create_session_app(adapter)
    body = {"choice": "once", "request_id": entry.data["request_id"]}

    async with TestClient(TestServer(app)) as cli:
        first, second = await asyncio.gather(
            cli.post(f"/api/sessions/{session_id}/approval", json=body),
            cli.post(f"/api/sessions/{session_id}/approval", json=body),
        )
        statuses = sorted([first.status, second.status])
        payloads = [await first.json(), await second.json()]

    assert statuses == [200, 409]
    winner = next(p for p in payloads if p.get("resolved"))
    loser = next(p for p in payloads if "error" in p)
    assert winner["resolved"] == 1
    assert loser["error"]["code"] == "approval_not_pending"
    assert entry.result == "once"


@pytest.mark.asyncio
async def test_session_stop_interrupts_running_turn(adapter, session_db):
    session_id = session_db.create_session(
        "stop-session", "discord", session_key="discord:chan-8"
    )
    agent = _StubAgent()
    adapter.gateway_runner = _StubRunner({"discord:chan-8": _StubSessionState(agent)})
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(f"/api/sessions/{session_id}/stop", json={})
        assert resp.status == 200, await resp.text()
        payload = await resp.json()

    assert payload == {
        "object": "hermes.session.stop",
        "session_id": session_id,
        "status": "stopping",
    }
    assert agent.interrupts == ["Stop requested via API"]


@pytest.mark.asyncio
async def test_session_stop_reports_not_running(adapter, session_db):
    """No running turn — and no session_key at all — are both honest no-ops."""
    running_id = session_db.create_session(
        "idle-session", "discord", session_key="discord:chan-9"
    )
    keyless_id = session_db.create_session("keyless-stop-session", "api_server")
    adapter.gateway_runner = _StubRunner({})
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        for session_id in (running_id, keyless_id):
            resp = await cli.post(f"/api/sessions/{session_id}/stop", json={})
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["status"] == "not_running"


@pytest.mark.asyncio
async def test_session_stop_unknown_session_404s(adapter):
    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/api/sessions/no-such-session/stop", json={})
        assert resp.status == 404, await resp.text()
        assert (await resp.json())["error"]["code"] == "session_not_found"


@pytest.mark.asyncio
async def test_list_approvals_maps_keys_to_sessions_and_admits_orphans(
    adapter, session_db, approval_queues
):
    """The global watch surface attributes what it can and admits what it can't.

    An approval whose routing key matches no session row still has to appear:
    an unattributable pending approval is precisely the one an operator needs
    to see, and dropping it would make the list quietly wrong.
    """
    session_id = session_db.create_session(
        "watch-session", "discord", session_key="discord:chan-10"
    )
    _enqueue_approval(approval_queues, "discord:chan-10", command="mapped")
    _enqueue_approval(approval_queues, "telegram:orphan", command="orphaned")
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/api/approvals")
        assert resp.status == 200, await resp.text()
        payload = await resp.json()

    assert payload["object"] == "hermes.approvals"
    by_command = {item["command"]: item for item in payload["approvals"]}
    assert by_command["mapped"]["session_id"] == session_id
    assert by_command["orphaned"]["session_id"] is None
    assert all("session_key" not in item for item in payload["approvals"])
    # Same redacted projection as the per-session list.
    assert by_command["mapped"]["choices"] == ["once", "session", "always", "deny"]


@pytest.mark.asyncio
async def test_list_approvals_empty_when_nothing_pending(adapter, approval_queues):
    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/api/approvals")
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["approvals"] == []
