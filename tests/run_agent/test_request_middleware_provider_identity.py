"""The request middleware sees exact selected identities, not just 'custom'."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.run_agent.test_provider_request_id_main_loop import (
    _anthropic_message, _make_agent, _no_network, _patch_bootstrap, _stream_cm,
)


@pytest.mark.parametrize('identity', ['custom:meridian-primary', 'custom:meridian-yugen', 'anthropic'])
def test_llm_request_middleware_receives_requested_provider(monkeypatch, identity):
    agent, _ = _make_agent(monkeypatch, api_mode='anthropic_messages',
                           provider='custom' if identity.startswith('custom:') else identity,
                           session_db=MagicMock())
    agent.requested_provider = identity
    agent._disable_streaming = True
    agent._interruptible_api_call = lambda kwargs: _anthropic_message()
    seen = []

    def middleware(request, **context):
        seen.append(context)
        return SimpleNamespace(payload=request, original_payload=request, changed=False, trace=[])

    monkeypatch.setattr('hermes_cli.middleware.apply_llm_request_middleware', middleware)
    result = agent.run_conversation('Synthetic identity propagation test')
    assert result['final_response'] == 'ok'
    assert len(seen) == 1
    assert seen[0]['provider'] == agent.provider
    assert seen[0]['requested_provider'] == identity


@pytest.mark.parametrize('identity', ['custom:meridian-primary', 'custom:meridian-yugen', 'anthropic'])
@pytest.mark.parametrize('stream', [False, True])
def test_acp_factory_identity_reaches_request_middleware(monkeypatch, identity, stream):
    import run_agent
    from acp_adapter.session import SessionManager

    _patch_bootstrap(monkeypatch)
    provider = MagicMock(name='offline-provider')
    monkeypatch.setattr('agent.anthropic_adapter.build_anthropic_client', lambda *args, **kwargs: provider)
    monkeypatch.setattr('hermes_cli.config.load_config', lambda: {
        'model': {'default': 'claude-haiku-4-5', 'provider': identity},
    })
    monkeypatch.setattr('hermes_cli.runtime_provider.resolve_runtime_provider', lambda **kwargs: {
        'provider': 'custom' if identity.startswith('custom:') else identity,
        'requested_provider': identity, 'api_mode': 'anthropic_messages',
        'base_url': 'http://127.0.0.1:9/v1', 'api_key': 'test-key',
    })
    monkeypatch.setattr('hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build', lambda **kwargs: None)

    class OfflineAgent(run_agent.AIAgent):
        def __init__(self, **kwargs):
            super().__init__(**{**kwargs, 'skip_context_files': True, 'skip_memory': True, 'max_iterations': 4})
            self._cleanup_task_resources = self._persist_session = lambda *args, **kwargs: None
            self._save_trajectory = lambda *args, **kwargs: None

    monkeypatch.setattr(run_agent, 'AIAgent', OfflineAgent)
    agent = SessionManager().create_session(cwd='/tmp').agent
    agent._create_request_anthropic_client = lambda *args, **kwargs: provider
    agent._disable_streaming = not stream
    agent._interruptible_api_call = lambda kwargs: _anthropic_message()
    provider.messages.stream.return_value = _stream_cm(_anthropic_message())
    seen = []

    def middleware(request, **context):
        seen.append(context)
        return SimpleNamespace(payload=request, original_payload=request, changed=False, trace=[])

    monkeypatch.setattr('hermes_cli.middleware.apply_llm_request_middleware', middleware)
    assert agent.run_conversation('Synthetic ACP factory identity test')['final_response'] == 'ok'
    assert seen[0]['requested_provider'] == identity
    assert seen[0]['provider'] == ('custom' if identity.startswith('custom:') else identity)
    if stream:
        provider.messages.stream.assert_called_once()
