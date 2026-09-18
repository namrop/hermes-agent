"""Authenticated operator reset delegates to the gateway's reset owner."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB

ROUTE = '/api/sessions/{session_id}/model/reset'
KEY = 'agent:main:discord:group:123:keeper'
SID = 'reset-api-session'


@pytest.fixture
def reset_adapter(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session(SID, 'discord')
    db.record_gateway_session_peer(
        SID, source='discord', user_id='keeper', session_key=KEY,
        chat_id='123', chat_type='group', thread_id=None,
    )
    a = APIServerAdapter(PlatformConfig(enabled=True, extra={'key': 'test-only-key'}))
    a._session_db = db
    a.gateway_runner = SimpleNamespace(
        session_store=object(),
        _lookup_session_id_under_store_lock=Mock(return_value=SID),
        _is_session_running=Mock(return_value=False),
        _reset_session_model_override=AsyncMock(),
    )
    yield a
    db.close()


def app_for(adapter, *, multiplex: bool = False):
    middlewares = [adapter._make_profile_prefix_middleware()] if multiplex else []
    app = web.Application(middlewares=middlewares)
    # Read the production route table: absent registration must fail RED as 404.
    app.router.add_get('/v1/capabilities', adapter._handle_capabilities)
    for method, path, handler in adapter._http_route_table():
        if path == ROUTE:
            app.router.add_route(method, path, handler)
            if multiplex:
                app.router.add_route(method, f'/p/{{profile}}{path}', handler)
    return app


async def post(adapter, sid=SID, auth=True):
    async with TestClient(TestServer(app_for(adapter))) as client:
        response = await client.post(
            '/api/sessions/'+sid+'/model/reset', json={},
            headers={'Authorization': 'Bearer test-only-key'} if auth else {},
        )
        return response.status, await response.text()


@pytest.mark.asyncio
async def test_model_reset_capability_is_discoverable(reset_adapter):
    async with TestClient(TestServer(app_for(reset_adapter))) as client:
        response = await client.get('/v1/capabilities', headers={'Authorization': 'Bearer test-only-key'})
        data = await response.json()
    assert data['endpoints'].get('session_model_reset') == {'method': 'POST', 'path': ROUTE}


@pytest.mark.asyncio
async def test_model_reset_calls_shared_owner_for_exact_gateway_conversation(reset_adapter):
    status, body = await post(reset_adapter)
    assert status == 200, body
    reset_adapter.gateway_runner._reset_session_model_override.assert_awaited_once_with(
        KEY,
        expected_session_id=SID,
    )
    assert 'hermes.session.model_reset' in body
    assert 'session_key' not in body


@pytest.mark.asyncio
async def test_model_reset_requires_authentication(reset_adapter):
    status, _ = await post(reset_adapter, auth=False)
    assert status == 401
    reset_adapter.gateway_runner._reset_session_model_override.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_reset_rejects_a_running_target_only(reset_adapter):
    reset_adapter.gateway_runner._is_session_running.return_value = True
    status, body = await post(reset_adapter)
    assert status == 409, body
    assert 'session_busy' in body
    reset_adapter.gateway_runner._reset_session_model_override.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_reset_rejects_stale_session_id(reset_adapter):
    reset_adapter.gateway_runner._lookup_session_id_under_store_lock.return_value = 'new-conversation'
    status, body = await post(reset_adapter)
    assert status == 409, body
    assert 'session_route_changed' in body
    reset_adapter.gateway_runner._reset_session_model_override.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_reset_reports_persistence_failure(reset_adapter):
    reset_adapter.gateway_runner._reset_session_model_override.side_effect = RuntimeError('disk unavailable')
    status, body = await post(reset_adapter)
    assert status == 500, body
    assert 'model_reset_failed' in body


@pytest.mark.asyncio
async def test_model_reset_missing_session_never_calls_owner(reset_adapter):
    status, _ = await post(reset_adapter, sid='missing')
    assert status == 404
    reset_adapter.gateway_runner._reset_session_model_override.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_reset_requires_configured_key_even_on_keyless_api(reset_adapter):
    reset_adapter._api_key = ""
    status, body = await post(reset_adapter, auth=False)
    assert status == 403, body
    assert 'model_reset_auth_required' in body
    reset_adapter.gateway_runner._reset_session_model_override.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_reset_does_not_cross_into_another_profile(reset_adapter):
    from gateway.platforms.api_server import _api_request_profile
    reset_adapter._check_auth = lambda request: None  # A valid profile-scoped key.
    token = _api_request_profile.set('other')
    try:
        status, body = await post(reset_adapter)
    finally:
        _api_request_profile.reset(token)
    assert status == 409, body
    assert 'model_reset_profile_unsupported' in body
    reset_adapter.gateway_runner._reset_session_model_override.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_reset_accepts_authenticated_default_profile_alias(reset_adapter, tmp_path, monkeypatch):
    """The production /p/default middleware targets the owning default gateway."""
    reset_adapter.gateway_runner.config = SimpleNamespace(
        multiplex_profiles=True,
        multiplex_profile_allowlist=None,
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex, profile_allowlist=None: [("default", tmp_path)],
    )
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda _name: tmp_path)

    async with TestClient(TestServer(app_for(reset_adapter, multiplex=True))) as client:
        response = await client.post(
            f'/p/default/api/sessions/{SID}/model/reset',
            json={},
            headers={'Authorization': 'Bearer test-only-key'},
        )
        status, body = response.status, await response.text()

    assert status == 200, body
    reset_adapter.gateway_runner._reset_session_model_override.assert_awaited_once_with(
        KEY,
        expected_session_id=SID,
    )


@pytest.mark.asyncio
async def test_model_reset_real_gateway_store_survives_fresh_load(tmp_path, monkeypatch):
    import hermes_state
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource, SessionStore

    db = SessionDB(tmp_path / 'integration.db')
    monkeypatch.setattr(hermes_state, 'SessionDB', lambda: db)
    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path / 'sessions', config=config)
    source = SessionSource(platform=Platform.DISCORD, chat_id='one', user_id='keeper', chat_type='group')
    other_source = SessionSource(platform=Platform.DISCORD, chat_id='two', user_id='keeper', chat_type='group')
    entry = store.get_or_create_session(source)
    other = store.get_or_create_session(other_source)
    override = {'model': 'manual-model', 'provider': 'openai-codex'}
    store.set_model_override(entry.session_key, override)
    store.set_model_override(other.session_key, override)
    db.replace_messages(entry.session_id, [{'role': 'user', 'content': 'keep history'}])
    before_messages = db.get_messages(entry.session_id)
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = store
    runner._session_model_overrides = {entry.session_key: dict(override), other.session_key: dict(override)}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._session_db = None
    runner._session_state(entry.session_key).conversation.personality_override = 'keeper-persona'
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={'key': 'test-only-key'}))
    adapter._session_db = db
    adapter.gateway_runner = runner
    try:
        status, body = await post(adapter, sid=entry.session_id)
        assert status == 200, body
        assert store.get_model_override(entry.session_key) is None
        assert runner._session_model_overrides.get(entry.session_key) is None
        assert runner._session_model_overrides[other.session_key] == override
        assert runner._session_state(entry.session_key).conversation.personality_override == 'keeper-persona'
        assert db.get_messages(entry.session_id) == before_messages
        cold = SessionStore(sessions_dir=tmp_path / 'sessions', config=config)
        assert cold.get_model_override(entry.session_key) is None
        assert cold.get_model_override(other.session_key) == override
    finally:
        db.close()


@pytest.mark.asyncio
async def test_model_reset_refuses_rebound_route_without_clearing_successor_override(tmp_path, monkeypatch):
    """A route rebound after the API lookup must not clear the successor's model."""
    import hermes_state
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource, SessionStore

    db = SessionDB(tmp_path / 'interleaving.db')
    monkeypatch.setattr(hermes_state, 'SessionDB', lambda: db)
    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path / 'sessions', config=config)
    source = SessionSource(platform=Platform.DISCORD, chat_id='one', user_id='keeper', chat_type='group')
    entry = store.get_or_create_session(source)
    old_override = {'model': 'old-model', 'provider': 'openai-codex'}
    successor_override = {'model': 'new-model', 'provider': 'openrouter'}
    store.set_model_override(entry.session_key, old_override)

    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = store
    runner._session_model_overrides = {entry.session_key: dict(old_override)}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._session_db = None
    runner._session_state(entry.session_key).conversation.model_override = dict(old_override)
    did_rotate = False

    def rotate_after_api_lookup(session_key):
        nonlocal did_rotate
        if not did_rotate:
            did_rotate = True
            successor = store.reset_session(session_key)
            assert successor is not None
            store.set_model_override(session_key, successor_override)
            runner._session_model_overrides[session_key] = dict(successor_override)
            runner._session_state(session_key).conversation.model_override = dict(successor_override)
        return False

    runner._is_session_running = rotate_after_api_lookup
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={'key': 'test-only-key'}))
    adapter._session_db = db
    adapter.gateway_runner = runner
    try:
        status, body = await post(adapter, sid=entry.session_id)
        successor_id = store.peek_session_id(entry.session_key)

        assert status == 409, body
        assert 'session_route_changed' in body
        assert 'hermes.session.model_reset' not in body
        assert successor_id is not None and successor_id != entry.session_id
        assert store.get_model_override(entry.session_key) == successor_override
        assert runner._session_model_overrides[entry.session_key] == successor_override
        assert runner._session_state(entry.session_key).conversation.model_override == successor_override
    finally:
        db.close()


@pytest.mark.asyncio
async def test_model_reset_refuses_newer_same_session_selection_before_durable_clear(tmp_path, monkeypatch):
    """A same-ID /model selection between reset read and CAS clear wins intact."""
    import hermes_state
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource, SessionStore

    db = SessionDB(tmp_path / 'same-id-interleaving.db')
    monkeypatch.setattr(hermes_state, 'SessionDB', lambda: db)
    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path / 'sessions', config=config)
    source = SessionSource(platform=Platform.DISCORD, chat_id='one', user_id='keeper', chat_type='group')
    entry = store.get_or_create_session(source)
    old_override = {'model': 'old-model', 'provider': 'openai-codex'}
    newer_override = {'model': 'new-model', 'provider': 'openrouter'}
    store.set_model_override(entry.session_key, old_override)

    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = store
    runner._session_model_overrides = {entry.session_key: dict(old_override)}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._session_db = None
    runner._session_state(entry.session_key).conversation.model_override = dict(old_override)
    real_async_store = runner.async_session_store
    did_select = False

    class NewerSelectionBeforeResetSave:
        def __init__(self):
            self._store = store

        async def get_durable_model_override(self, session_key):
            return await real_async_store.get_durable_model_override(session_key)

        async def set_model_override(self, session_key, override, **kwargs):
            nonlocal did_select
            if override is None and not did_select:
                did_select = True
                store.set_model_override(session_key, newer_override)
                runner._session_model_overrides[session_key] = dict(newer_override)
                runner._session_state(session_key).conversation.model_override = dict(newer_override)
            return await real_async_store.set_model_override(session_key, override, **kwargs)

    runner._async_session_store = NewerSelectionBeforeResetSave()
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={'key': 'test-only-key'}))
    adapter._session_db = db
    adapter.gateway_runner = runner
    try:
        status, body = await post(adapter, sid=entry.session_id)

        assert status == 409, body
        assert 'model_reset_superseded' in body
        assert 'hermes.session.model_reset' not in body
        assert store.peek_session_id(entry.session_key) == entry.session_id
        assert store.get_model_override(entry.session_key) == newer_override
        assert runner._session_model_overrides[entry.session_key] == newer_override
        assert runner._session_state(entry.session_key).conversation.model_override == newer_override
    finally:
        db.close()


@pytest.mark.asyncio
async def test_model_reset_refuses_route_rebound_after_durable_readback(tmp_path, monkeypatch):
    """A successor that arrives after reset readback must keep its volatile route."""
    import hermes_state
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource, SessionStore

    db = SessionDB(tmp_path / 'post-readback-interleaving.db')
    monkeypatch.setattr(hermes_state, 'SessionDB', lambda: db)
    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path / 'sessions', config=config)
    source = SessionSource(platform=Platform.DISCORD, chat_id='one', user_id='keeper', chat_type='group')
    entry = store.get_or_create_session(source)
    old_override = {'model': 'old-model', 'provider': 'openai-codex'}
    successor_override = {'model': 'new-model', 'provider': 'openrouter'}
    store.set_model_override(entry.session_key, old_override)

    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = store
    runner._session_model_overrides = {entry.session_key: dict(old_override)}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._session_db = None
    runner._session_state(entry.session_key).conversation.model_override = dict(old_override)
    real_async_store = runner.async_session_store
    durable_reads = 0

    class ReboundAfterDurableReadback:
        def __init__(self):
            self._store = store

        async def get_durable_model_override(self, session_key):
            nonlocal durable_reads
            result = await real_async_store.get_durable_model_override(session_key)
            durable_reads += 1
            if durable_reads == 2:
                successor = store.reset_session(session_key)
                assert successor is not None
                store.set_model_override(session_key, successor_override)
                runner._session_model_overrides[session_key] = dict(successor_override)
                runner._session_state(session_key).conversation.model_override = dict(successor_override)
            return result

        async def set_model_override(self, session_key, override, **kwargs):
            return await real_async_store.set_model_override(session_key, override, **kwargs)

    runner._async_session_store = ReboundAfterDurableReadback()
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={'key': 'test-only-key'}))
    adapter._session_db = db
    adapter.gateway_runner = runner
    try:
        status, body = await post(adapter, sid=entry.session_id)

        assert status == 409, body
        assert 'session_route_changed' in body
        assert 'hermes.session.model_reset' not in body
        assert store.peek_session_id(entry.session_key) != entry.session_id
        assert store.get_model_override(entry.session_key) == successor_override
        assert runner._session_model_overrides[entry.session_key] == successor_override
        assert runner._session_state(entry.session_key).conversation.model_override == successor_override
    finally:
        db.close()


@pytest.mark.asyncio
async def test_model_reset_requires_gateway_route(reset_adapter):
    reset_adapter._session_db.create_session('browser-only', 'api_server')
    status, body = await post(reset_adapter, sid='browser-only')
    assert status == 409, body
    assert 'session_not_gateway_routed' in body
    reset_adapter.gateway_runner._reset_session_model_override.assert_not_awaited()
