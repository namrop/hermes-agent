"""Vision recovery keeps provider and model together (no network calls)."""

import asyncio
import json
from types import SimpleNamespace

import pytest

import agent.auxiliary_client as aux
from hermes_constants import get_hermes_home


@pytest.fixture
def wiring(monkeypatch):
    sent = []
    accounted = []
    unavailable = {"openai-codex"}
    routes = {
        "custom:primary": ("http://main.invalid", "claude-opus-5-5"),
        "custom:fallback": ("http://fallback.invalid", "claude-opus-5-5"),
        "custom:local": ("http://local.invalid", "local-vision"),
        "openai-codex": ("http://codex.invalid", "gpt-6-sol"),
    }

    class Client:
        def __init__(self, base_url, async_mode=False, **kwargs):
            self.base_url = base_url
            self.api_key = "synthetic-fixture"

            def create(**request):
                sent.append((self.base_url, request))
                return SimpleNamespace(
                    model=request["model"],
                    usage=None,
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content="image described", tool_calls=None
                            ),
                            finish_reason="stop",
                        )
                    ],
                )

            async def acreate(**request):
                return create(**request)

            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=acreate if async_mode else create)
            )

    def factory(provider, model=None, async_mode=False, **kwargs):
        if provider in unavailable:
            return None, None
        base, default = routes.get(
            provider, ("http://unused.invalid", "fixture-default")
        )
        return Client(
            kwargs.get("explicit_base_url") or base, async_mode
        ), model or default

    # Replace only client construction/transport; use the real task, chain,
    # auto resolver, async conversion, call kwargs and response validation.
    monkeypatch.setattr(aux, "resolve_provider_client", factory)
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **kw: Client(kw["base_url"], True))
    monkeypatch.setattr(aux, "_main_model_supports_vision", lambda *a: True)
    monkeypatch.setattr(aux, "_resolve_provider_vision_default", lambda *a: None)
    monkeypatch.setattr(aux, "_openai_http_client_kwargs", lambda *a, **kw: {})
    monkeypatch.setattr(
        "agent.aux_accounting.record_aux_usage",
        lambda response, task, **kw: accounted.append(kw),
    )
    # Metadata resolution must not consult real provider credentials.
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kw: {"api_mode": "chat_completions"},
    )
    aux._client_cache.clear()
    yield sent, accounted, unavailable
    aux._client_cache.clear()


def configure(chain):
    (get_hermes_home() / "config.yaml").write_text(
        json.dumps({
            "auxiliary": {
                "vision": {
                    "provider": "openai-codex",
                    "model": "gpt-6-sol",
                    "fallback_chain": chain,
                }
            }
        }),
        encoding="utf-8",
    )


def invoke(async_mode, **kwargs):
    args = dict(
        task="vision",
        messages=[{"role": "user", "content": "describe fixture"}],
        main_runtime={
            "provider": "custom:primary",
            "model": "claude-opus-5-5",
            "base_url": "http://main.invalid",
            "api_key": "synthetic-fixture",
            "api_mode": "chat_completions",
        },
    )
    args.update(kwargs)
    return (
        asyncio.run(aux.async_call_llm(**args)) if async_mode else aux.call_llm(**args)
    )


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("first_unavailable", [False, True])
def test_unavailable_vision_uses_configured_chain_pair(
    wiring, async_mode, first_unavailable
):
    sent, accounted, unavailable = wiring
    configure([
        {
            "provider": "custom:fallback",
            "model": "claude-opus-5-5",
            "api_mode": "chat_completions",
        },
        {
            "provider": "custom:local",
            "model": "local-vision",
            "api_mode": "chat_completions",
        },
    ])
    if first_unavailable:
        unavailable.add("custom:fallback")
    route = {}
    result = invoke(async_mode, route_info=route)
    expected = (
        ("custom:local", "http://local.invalid", "local-vision")
        if first_unavailable
        else ("custom:fallback", "http://fallback.invalid", "claude-opus-5-5")
    )
    assert result.choices[0].message.content == "image described"
    assert len(sent) == 1
    assert (sent[0][0], sent[0][1]["model"]) == expected[1:]
    assert accounted[-1]["provider"] == expected[0]
    assert route["provider"] == expected[0]


@pytest.mark.parametrize("async_mode", [False, True])
def test_auto_recovery_does_not_reload_failed_task_model(wiring, async_mode):
    sent, accounted, _ = wiring
    configure([])
    invoke(async_mode)
    assert (sent[0][0], sent[0][1]["model"]) == (
        "http://main.invalid",
        "claude-opus-5-5",
    )
    assert accounted[-1]["provider"] == "custom:primary"


@pytest.mark.parametrize("async_mode", [False, True])
def test_available_primary_keeps_its_model(wiring, async_mode):
    sent, _, unavailable = wiring
    unavailable.clear()
    configure([{"provider": "custom:fallback", "model": "claude-opus-5-5"}])
    invoke(async_mode)
    assert (sent[0][0], sent[0][1]["model"]) == ("http://codex.invalid", "gpt-6-sol")


@pytest.mark.parametrize("async_mode", [False, True])
def test_recovered_route_uses_destination_wire_mode(wiring, monkeypatch, async_mode):
    configure([
        {
            "provider": "custom:fallback",
            "model": "claude-opus-5-5",
            "api_mode": "anthropic_messages",
        }
    ])
    observed = []
    original = aux._set_relay_auxiliary_route

    def observe(provider, model, api_mode):
        observed.append((provider, model, api_mode))
        return original(provider, model, api_mode)

    monkeypatch.setattr(aux, "_set_relay_auxiliary_route", observe)
    invoke(async_mode)
    assert observed[-1] == ("custom:fallback", "claude-opus-5-5", "anthropic_messages")


def test_explicit_auto_model_override_still_works(wiring):
    sent, _, _ = wiring
    configure([])
    invoke(False, provider="auto", model="deliberately-selected")
    assert sent[0][1]["model"] == "deliberately-selected"


@pytest.mark.parametrize("async_mode", [False, True])
def test_unavailable_explicit_endpoint_does_not_use_other_routes(wiring, async_mode):
    sent, _, unavailable = wiring
    unavailable.add("custom")
    configure([{"provider": "custom:fallback", "model": "claude-opus-5-5"}])
    with pytest.raises(RuntimeError, match="No LLM provider configured"):
        invoke(
            async_mode,
            provider="custom",
            model="direct-model",
            base_url="http://explicit.invalid",
        )
    assert not sent


@pytest.mark.parametrize("async_mode", [False, True])
def test_explicit_endpoint_is_preserved(wiring, async_mode):
    sent, _, _ = wiring
    configure([])
    invoke(
        async_mode,
        provider="custom:primary",
        model="direct-model",
        base_url="http://explicit.invalid",
    )
    assert (sent[0][0], sent[0][1]["model"]) == (
        "http://explicit.invalid",
        "direct-model",
    )
