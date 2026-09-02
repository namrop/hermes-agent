"""Render-profile seam, HTTP side.

A client discovers the available renderer contracts from
``GET /v1/capabilities``, selects one at ``POST /api/sessions`` or on the
first chat turn, and is then pinned to it for the life of the session. The
pin is what keeps the system-prompt prefix — and therefore the provider
prompt-cache key — stable across turns.

Pinned here: the manifest, 400 on an unknown profile, 409 on a mid-session
change, the value surviving a reload of the session row, and the default
staying ``plain`` so every pre-existing API client is untouched.
"""

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


def _render_profile_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_get("/v1/capabilities", adapter._handle_capabilities)
    app.router.add_post("/api/sessions", adapter._handle_create_session)
    app.router.add_get("/api/sessions/{session_id}", adapter._handle_get_session)
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    app.router.add_post(
        "/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream
    )
    return app


def _chat_mock(session_id: str) -> AsyncMock:
    return AsyncMock(
        return_value=({"final_response": "ok", "session_id": session_id}, {"total_tokens": 1})
    )


# ---------------------------------------------------------------- capabilities


@pytest.mark.asyncio
async def test_capabilities_advertises_render_profiles(adapter):
    app = _render_profile_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/v1/capabilities")
        assert resp.status == 200
        data = await resp.json()

    assert data["render_profiles"] == ["plain", "diadem-native-v1"]
    assert data["render_profile_default"] == "plain"
    assert data["features"]["render_profiles"] is True


@pytest.mark.asyncio
async def test_capabilities_states_media_interception_is_profile_independent(adapter):
    """Item 5 of the seam: markdown support and media delivery are separate
    questions, and the manifest says which endpoints intercept ``MEDIA:``."""
    app = _render_profile_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        data = await (await cli.get("/v1/capabilities")).json()

    note = data["render_profile_note"]
    assert "render_profile_pinned" in note
    assert "MEDIA:" in note
    assert "/v1/runs" in note


# ---------------------------------------------------------------- session create


@pytest.mark.asyncio
async def test_create_session_defaults_to_plain_and_reports_it_unpinned(adapter):
    app = _render_profile_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/api/sessions", json={"id": "rp-default"})
        assert resp.status == 201
        session = (await resp.json())["session"]

    assert session["render_profile"] == "plain"
    assert session["render_profile_pinned"] is False


@pytest.mark.asyncio
async def test_create_session_accepts_and_echoes_a_valid_profile(adapter):
    app = _render_profile_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/sessions",
            json={"id": "rp-diadem", "render_profile": "diadem-native-v1"},
        )
        assert resp.status == 201
        session = (await resp.json())["session"]

    assert session["render_profile"] == "diadem-native-v1"
    assert session["render_profile_pinned"] is True


@pytest.mark.asyncio
async def test_create_session_rejects_an_unknown_profile(adapter, session_db):
    app = _render_profile_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/sessions", json={"id": "rp-bogus", "render_profile": "markdown"}
        )
        assert resp.status == 400
        payload = await resp.json()

    assert payload["error"]["code"] == "invalid_render_profile"
    assert "diadem-native-v1" in payload["error"]["message"]
    # Validation fires before the insert: no half-created row is left behind.
    assert session_db.get_session("rp-bogus") is None


@pytest.mark.asyncio
async def test_create_session_rejects_a_non_string_profile(adapter):
    app = _render_profile_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/sessions", json={"id": "rp-nonstring", "render_profile": 7}
        )
        assert resp.status == 400
        assert (await resp.json())["error"]["code"] == "invalid_render_profile"


@pytest.mark.asyncio
async def test_stored_profile_survives_a_reload_of_the_session_row(adapter, session_db):
    """Restart safety: the profile lives in the ``model_config`` JSON column,
    so a fresh read of the row (and a fresh adapter) still sees it."""
    app = _render_profile_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/sessions",
            json={"id": "rp-durable", "render_profile": "diadem-native-v1"},
        )
        assert resp.status == 201

    # Straight out of the DB, no adapter in the way.
    row = session_db.get_session("rp-durable")
    assert SessionDB.session_render_profile(row) == "diadem-native-v1"

    # And through a brand-new adapter reading the same file, as a restart would.
    reloaded = APIServerAdapter(PlatformConfig(enabled=True))
    reloaded._session_db = SessionDB(session_db.db_path)
    try:
        async with TestClient(TestServer(_render_profile_app(reloaded))) as cli:
            session = (await (await cli.get("/api/sessions/rp-durable")).json())["session"]
    finally:
        reloaded._session_db.close()

    assert session["render_profile"] == "diadem-native-v1"
    assert session["render_profile_pinned"] is True


@pytest.mark.asyncio
async def test_profile_does_not_clobber_a_browser_model_lock(adapter, session_db):
    """Both settings share the ``model_config`` JSON column; neither may
    overwrite the other."""
    app = _render_profile_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/sessions",
            json={
                "id": "rp-with-lock",
                "render_profile": "diadem-native-v1",
                "model": "some/model",
                "provider": "openrouter",
                "require_model_lock": True,
            },
        )
        assert resp.status == 201

    row = session_db.get_session("rp-with-lock")
    config = APIServerAdapter._parse_session_model_config(row.get("model_config"))
    assert config["render_profile"] == "diadem-native-v1"
    assert config["browser_model_lock"]["model"] == "some/model"


# ---------------------------------------------------------------- chat turns


@pytest.mark.asyncio
async def test_chat_without_a_profile_runs_plain(adapter, session_db):
    """The untouched default: every pre-existing API client keeps today's
    behaviour."""
    session_id = session_db.create_session("rp-chat-default", "api_server")
    mock_run = _chat_mock(session_id)
    async with TestClient(TestServer(_render_profile_app(adapter))) as cli:
        with patch.object(adapter, "_run_agent", mock_run):
            resp = await cli.post(f"/api/sessions/{session_id}/chat", json={"message": "hi"})
            assert resp.status == 200

    assert mock_run.call_args.kwargs["render_profile"] == "plain"


@pytest.mark.asyncio
async def test_first_turn_pins_the_profile_and_persists_it(adapter, session_db):
    session_id = session_db.create_session("rp-chat-pin", "api_server")
    mock_run = _chat_mock(session_id)
    async with TestClient(TestServer(_render_profile_app(adapter))) as cli:
        with patch.object(adapter, "_run_agent", mock_run):
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={"message": "hi", "render_profile": "diadem-native-v1"},
            )
            assert resp.status == 200

    assert mock_run.call_args.kwargs["render_profile"] == "diadem-native-v1"
    assert (
        SessionDB.session_render_profile(session_db.get_session(session_id))
        == "diadem-native-v1"
    )


@pytest.mark.asyncio
async def test_later_turns_inherit_the_pinned_profile_without_resending_it(
    adapter, session_db
):
    session_id = session_db.create_session("rp-chat-inherit", "api_server")
    session_db.set_session_render_profile(session_id, "diadem-native-v1")
    mock_run = _chat_mock(session_id)
    async with TestClient(TestServer(_render_profile_app(adapter))) as cli:
        with patch.object(adapter, "_run_agent", mock_run):
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat", json={"message": "turn 2"}
            )
            assert resp.status == 200

    assert mock_run.call_args.kwargs["render_profile"] == "diadem-native-v1"


@pytest.mark.asyncio
async def test_resending_the_same_profile_is_idempotent(adapter, session_db):
    session_id = session_db.create_session("rp-chat-same", "api_server")
    session_db.set_session_render_profile(session_id, "diadem-native-v1")
    mock_run = _chat_mock(session_id)
    async with TestClient(TestServer(_render_profile_app(adapter))) as cli:
        with patch.object(adapter, "_run_agent", mock_run):
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={"message": "again", "render_profile": "diadem-native-v1"},
            )
            assert resp.status == 200

    assert mock_run.call_args.kwargs["render_profile"] == "diadem-native-v1"


@pytest.mark.asyncio
async def test_changing_the_profile_mid_session_is_409_pinned(adapter, session_db):
    """This 409 IS the prompt-cache-stability guarantee. Without it a client
    could move the cached system-prompt prefix under a live session."""
    session_id = session_db.create_session("rp-chat-conflict", "api_server")
    session_db.set_session_render_profile(session_id, "diadem-native-v1")
    mock_run = _chat_mock(session_id)
    async with TestClient(TestServer(_render_profile_app(adapter))) as cli:
        with patch.object(adapter, "_run_agent", mock_run):
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={"message": "switch", "render_profile": "plain"},
            )
            assert resp.status == 409
            payload = await resp.json()

    assert payload["error"]["code"] == "render_profile_pinned"
    mock_run.assert_not_called()
    # And the pin held.
    assert (
        SessionDB.session_render_profile(session_db.get_session(session_id))
        == "diadem-native-v1"
    )


@pytest.mark.asyncio
async def test_unknown_profile_on_a_chat_turn_is_400(adapter, session_db):
    session_id = session_db.create_session("rp-chat-bogus", "api_server")
    mock_run = _chat_mock(session_id)
    async with TestClient(TestServer(_render_profile_app(adapter))) as cli:
        with patch.object(adapter, "_run_agent", mock_run):
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={"message": "hi", "render_profile": "html"},
            )
            assert resp.status == 400
            assert (await resp.json())["error"]["code"] == "invalid_render_profile"

    mock_run.assert_not_called()
    assert SessionDB.session_render_profile(session_db.get_session(session_id)) == ""


# ---------------------------------------------------------------- streaming twin


@pytest.mark.asyncio
async def test_stream_first_turn_pins_the_profile(adapter, session_db):
    session_id = session_db.create_session("rp-stream-pin", "api_server")
    mock_run = _chat_mock(session_id)
    async with TestClient(TestServer(_render_profile_app(adapter))) as cli:
        with patch.object(adapter, "_run_agent", mock_run):
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat/stream",
                json={"message": "hi", "render_profile": "diadem-native-v1"},
            )
            assert resp.status == 200
            await resp.read()

    assert mock_run.call_args.kwargs["render_profile"] == "diadem-native-v1"
    assert (
        SessionDB.session_render_profile(session_db.get_session(session_id))
        == "diadem-native-v1"
    )


@pytest.mark.asyncio
async def test_stream_rejects_a_mid_session_change_with_409(adapter, session_db):
    session_id = session_db.create_session("rp-stream-conflict", "api_server")
    session_db.set_session_render_profile(session_id, "plain")
    mock_run = _chat_mock(session_id)
    async with TestClient(TestServer(_render_profile_app(adapter))) as cli:
        with patch.object(adapter, "_run_agent", mock_run):
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat/stream",
                json={"message": "switch", "render_profile": "diadem-native-v1"},
            )
            assert resp.status == 409
            assert (await resp.json())["error"]["code"] == "render_profile_pinned"

    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_stream_rejects_an_unknown_profile_with_400(adapter, session_db):
    session_id = session_db.create_session("rp-stream-bogus", "api_server")
    mock_run = _chat_mock(session_id)
    async with TestClient(TestServer(_render_profile_app(adapter))) as cli:
        with patch.object(adapter, "_run_agent", mock_run):
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat/stream",
                json={"message": "hi", "render_profile": "nope"},
            )
            assert resp.status == 400
            assert (await resp.json())["error"]["code"] == "invalid_render_profile"

    mock_run.assert_not_called()


# ---------------------------------------------------------------- agent stash


@pytest.mark.parametrize(
    "sent,expected",
    [
        ("diadem-native-v1", "diadem-native-v1"),
        ("plain", "plain"),
        ("", "plain"),
        ("bogus", "plain"),
    ],
)
@patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", True)
def test_create_agent_normalizes_the_profile_onto_the_agent(sent, expected):
    """``_create_agent`` stashes the resolved profile on the AIAgent the same
    way ``_hermes_api_runtime`` is stashed; ``agent/system_prompt.py`` reads it
    back when it selects the api_server platform hint. Normalization here is
    belt-and-braces: the HTTP boundary already 400s an unknown id, but nothing
    downstream may ever widen the declared renderer."""
    adapter = APIServerAdapter(PlatformConfig())

    with patch("gateway.run._resolve_runtime_agent_kwargs") as mock_kwargs, \
         patch("gateway.run._resolve_gateway_model") as mock_model, \
         patch("gateway.run._load_gateway_config") as mock_config, \
         patch("run_agent.AIAgent") as mock_agent_cls:
        mock_kwargs.return_value = {
            "api_key": "test-key", "base_url": None, "provider": None,
            "api_mode": None, "command": None, "args": [],
        }
        mock_model.return_value = "test/model"
        mock_config.return_value = {}
        mock_agent_cls.return_value = MagicMock()

        agent = adapter._create_agent(render_profile=sent)

    assert agent.render_profile == expected
    assert mock_agent_cls.call_args.kwargs["platform"] == "api_server"
    # The profile is a post-construction stash, NOT an AIAgent.__init__ kwarg.
    assert "render_profile" not in mock_agent_cls.call_args.kwargs


@pytest.mark.asyncio
async def test_run_agent_forwards_the_profile_to_create_agent(adapter):
    """The middle hop of the plumbing: handler -> _run_agent -> _create_agent."""
    with patch.object(adapter, "_create_agent") as mock_create:
        mock_create.return_value = MagicMock(
            run_conversation=MagicMock(return_value={"final_response": "ok"}),
            session_id="s",
        )
        await adapter._run_agent(
            user_message="hi",
            conversation_history=[],
            session_id="s",
            render_profile="diadem-native-v1",
        )

    assert mock_create.call_args.kwargs["render_profile"] == "diadem-native-v1"


@pytest.mark.asyncio
async def test_run_agent_defaults_to_plain_for_sessionless_endpoints(adapter):
    """/v1/chat/completions, /v1/responses and /v1/runs have no session row to
    negotiate on, so they never pass a profile and must stay byte-identical to
    the pre-seam behaviour."""
    with patch.object(adapter, "_create_agent") as mock_create:
        mock_create.return_value = MagicMock(
            run_conversation=MagicMock(return_value={"final_response": "ok"}),
            session_id="s",
        )
        await adapter._run_agent(
            user_message="hi", conversation_history=[], session_id="s"
        )

    assert mock_create.call_args.kwargs["render_profile"] == ""
