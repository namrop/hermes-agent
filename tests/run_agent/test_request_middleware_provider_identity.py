"""The request middleware sees exact selected identities, not just 'custom'."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.run_agent.test_provider_request_id_main_loop import (
    _anthropic_message, _make_agent, _no_network,
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
