"""The main loop hands each call's provider response id to the usage writer.

Usage contract v2 (keeper ratified 2026-09-25, Discord #gateway unified usage
ledger thread). Keeper, msg 1552935507678199862: "the hermes test should be
written too, before switch, but don't do a live provider call please. use a
mock provider".

The union ledger joins a Hermes call through Meridian to the Claude SDK's
receipt of the same call by Anthropic's message id. The id reaches the
sidecar through ONE keyword at the main-loop persistence call site
(``provider_request_id=_provider_request_id_for(...)`` in
agent/conversation_loop.py). The sidecar and selection tests do not cover
that line, so an upstream merge could drop it and the double count would
return with no failing test. These tests drive a whole ``run_conversation``
turn against a mock Anthropic provider (no network) and assert what the
session DB receives, on both paths:

* streaming, the production path for Meridian: the id comes from the
  stream's final message;
* non-streaming.

Negative cases keep the selection honest end to end: an id-less response and
a chat-completions id record nothing.
"""

import socket
import sys
import traceback
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

import run_agent

ANTHROPIC_ID = "msg_011CfPfSeZnPyZipskzk2go5"  # the id shape verified live on Meridian 2026-09-25


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Keeper: no live provider call. Any TCP connect attempt fails the test."""
    attempts = []
    real_connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            callers = [
                f"{frame.filename.rsplit('/', 1)[-1]}:{frame.name}"
                for frame in traceback.extract_stack()[:-1]
                if "site-packages" not in frame.filename and "/lib/python3" not in frame.filename
            ]
            attempts.append((address, " <- ".join(reversed(callers[-3:]))))
            raise OSError(f"network disabled in this test: {address!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    _stub_metadata_lookups(monkeypatch)
    yield
    assert not attempts, f"test attempted network connections: {attempts}"


# Agent init and cost accounting look up context length and pricing over the
# network (OpenRouter catalog, local-server probes of base_url). None of it is
# the model call under test; stub every binding so the turn is fully offline.
_METADATA_STUBS = {
    "fetch_model_metadata": {},
    "fetch_endpoint_model_metadata": {},
    "detect_local_server_type": None,
    "query_ollama_num_ctx": None,
    "_query_local_context_length": None,
    "_query_ollama_api_show": None,
}


def _stub_metadata_lookups(monkeypatch):
    import agent.model_metadata as model_metadata

    for name, value in _METADATA_STUBS.items():
        original = getattr(model_metadata, name)
        for module in list(sys.modules.values()):
            if module is not None and getattr(module, name, None) is original:
                monkeypatch.setattr(module, name, lambda *a, _value=value, **k: _value)


def _patch_bootstrap(monkeypatch):
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda **kwargs: [{
        "type": "function",
        "function": {"name": "t", "description": "t", "parameters": {"type": "object", "properties": {}}},
    }])
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})


def _stream_cm(final_message):
    stream = MagicMock()
    stream.__iter__ = MagicMock(return_value=iter([]))
    stream.get_final_message = MagicMock(return_value=final_message)
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=stream)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _anthropic_message(message_id=ANTHROPIC_ID):
    fields = dict(
        type="message",
        role="assistant",
        content=[SimpleNamespace(type="text", text="ok")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=546, output_tokens=37),
        model="claude-haiku-4-5-20251001",
    )
    if message_id is not None:
        fields["id"] = message_id
    return SimpleNamespace(**fields)


def _make_agent(monkeypatch, *, api_mode, provider, session_db):
    _patch_bootstrap(monkeypatch)
    mock_provider = MagicMock(name="mock-anthropic-provider")
    if api_mode == "anthropic_messages":
        monkeypatch.setattr(
            "agent.anthropic_adapter.build_anthropic_client",
            lambda k, b=None, **kwargs: mock_provider,
        )

    class _A(run_agent.AIAgent):
        def __init__(self, *a, **kw):
            kw.update(skip_context_files=True, skip_memory=True, max_iterations=4)
            super().__init__(*a, **kw)
            self._cleanup_task_resources = self._persist_session = lambda *a, **k: None
            self._save_trajectory = lambda *a, **k: None

    agent = _A(
        model="claude-haiku-4-5",
        api_key="test-key",
        base_url="http://127.0.0.1:9/v1",  # unroutable; every call below is mocked
        provider=provider,
        api_mode=api_mode,
        session_db=session_db,
        session_id="sess-v2",
        platform="discord",
    )
    agent._anthropic_client = mock_provider
    agent._create_request_anthropic_client = lambda *a, **k: mock_provider
    return agent, mock_provider


def _recorded_request_ids(session_db):
    calls = session_db.queue_token_counts.call_args_list
    assert calls, "the turn persisted no token delta"
    return [call.kwargs.get("provider_request_id") for call in calls]


def test_streaming_turn_records_the_anthropic_message_id(monkeypatch):
    """Production path for Meridian: streamed Messages call, real client."""
    session_db = MagicMock()
    agent, provider = _make_agent(
        monkeypatch, api_mode="anthropic_messages", provider="custom:meridian-primary", session_db=session_db,
    )
    provider.messages.stream = MagicMock(return_value=_stream_cm(_anthropic_message()))
    assert not isinstance(getattr(agent, "client", None), MagicMock)  # keeps the streaming branch

    result = agent.run_conversation("hi")

    assert result["final_response"] == "ok"
    provider.messages.stream.assert_called()  # the streaming path actually ran
    assert _recorded_request_ids(session_db) == [ANTHROPIC_ID]


def test_non_streaming_turn_records_the_anthropic_message_id(monkeypatch):
    session_db = MagicMock()
    agent, _ = _make_agent(
        monkeypatch, api_mode="anthropic_messages", provider="custom:meridian-yugen", session_db=session_db,
    )
    agent._disable_streaming = True
    agent._interruptible_api_call = lambda kw: _anthropic_message()

    agent.run_conversation("hi")

    assert _recorded_request_ids(session_db) == [ANTHROPIC_ID]


def test_response_without_an_id_records_none(monkeypatch):
    session_db = MagicMock()
    agent, _ = _make_agent(
        monkeypatch, api_mode="anthropic_messages", provider="custom:meridian-primary", session_db=session_db,
    )
    agent._disable_streaming = True
    agent._interruptible_api_call = lambda kw: _anthropic_message(message_id=None)

    agent.run_conversation("hi")

    assert _recorded_request_ids(session_db) == [None]


def test_chat_completions_id_is_not_recorded(monkeypatch):
    session_db = MagicMock()
    agent, _ = _make_agent(monkeypatch, api_mode="chat_completions", provider="openrouter", session_db=session_db)
    agent._disable_streaming = True
    agent._interruptible_api_call = lambda kw: SimpleNamespace(
        id="chatcmpl-abc123",
        choices=[SimpleNamespace(index=0, message=SimpleNamespace(
            role="assistant", content="ok", tool_calls=None, reasoning_content=None,
        ), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=50, completion_tokens=5, total_tokens=55),
        model="gpt-4o",
    )

    agent.run_conversation("hi")

    assert _recorded_request_ids(session_db) == [None]
