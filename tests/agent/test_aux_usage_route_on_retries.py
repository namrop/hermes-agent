"""Aux retries and fallback hops record the route that served them.

Keeper, Discord #gateway msg 1555103642531397743 (2026-10-01): "yes do that
please", for the aux half of the usage-label work. Before this change only the
first-attempt success path passed provider/base_url to the accounting
chokepoint; every same-provider retry and every fallback-chain hop recorded an
empty route (104 rows in 14 days, 103 of them smart approvals), which can be
neither priced nor attributed.

These tests drive the real ``call_llm`` / ``async_call_llm`` with scripted
fixture clients (no network) through each retry class and a configured chain
hop, and assert the provider and base URL that reach accounting. One test
writes through the real SessionDB and sidecar.
"""

import asyncio
import json
import socket
import sqlite3
from types import SimpleNamespace

import pytest

import agent.auxiliary_client as aux
import agent.usage_events as events
from agent.aux_accounting import reset_accounting_context, set_accounting_context
from hermes_constants import get_hermes_home
from hermes_state import SessionDB

ZAI = "https://api.z.ai/fixture/v4"
OMLX = "https://omlx.acubens.pharos.zone/v1"
ROUTES = {
    "zai": (ZAI, "glm-5.3"),
    "custom:omlx": (OMLX, "Qwen3.8-35B"),
}


class ConnectionFixtureError(Exception):
    """Type name carries 'Connection': a transient transport blip."""


class RateLimitError(Exception):
    """Type name marks a rate limit: a capacity error that falls back."""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No live provider calls: any TCP connect attempt fails the test."""
    attempts = []
    real_connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            attempts.append(address)
            raise OSError(f"network disabled in this test: {address!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    yield
    assert not attempts, f"test attempted network connections: {attempts}"


def _response(model):
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12),
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="approved", tool_calls=None),
                finish_reason="stop",
            )
        ],
    )


@pytest.fixture
def wiring(monkeypatch):
    """script[provider] is a list of outcomes consumed per call; an exception
    instance is raised, anything else means success. Empty list = success."""
    script = {"zai": [], "custom:omlx": []}
    sent = []
    accounted = []

    class Client:
        def __init__(self, provider, base_url, async_mode=False):
            self.base_url = base_url
            self.api_key = "synthetic-fixture"

            def create(**request):
                sent.append((provider, self.base_url, dict(request)))
                queue = script[provider]
                outcome = queue.pop(0) if queue else None
                if isinstance(outcome, Exception):
                    raise outcome
                return _response(request["model"])

            async def acreate(**request):
                return create(**request)

            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=acreate if async_mode else create)
            )

    def factory(provider, model=None, async_mode=False, **kwargs):
        if provider not in ROUTES:
            return None, None
        base, default = ROUTES[provider]
        return Client(provider, kwargs.get("explicit_base_url") or base, async_mode), model or default

    def async_from_base(**kw):
        """Sync-to-async conversion builds openai.AsyncOpenAI(base_url=...)."""
        base = str(kw.get("base_url") or "").rstrip("/")
        provider = next(p for p, (url, _) in ROUTES.items() if url.rstrip("/") == base)
        return Client(provider, kw.get("base_url"), True)

    monkeypatch.setattr(aux, "resolve_provider_client", factory)
    monkeypatch.setattr("openai.AsyncOpenAI", async_from_base)
    monkeypatch.setattr(aux, "_openai_http_client_kwargs", lambda *a, **kw: {})
    monkeypatch.setattr(aux, "_TRANSIENT_RETRY_BACKOFF_BASE", 0.0)
    # Aux accounting prices each row; for a custom endpoint the estimator
    # fetches the endpoint's model metadata. Keep it offline.
    monkeypatch.setattr("agent.usage_pricing.fetch_endpoint_model_metadata", lambda *a, **kw: {})
    monkeypatch.setattr("agent.usage_pricing.fetch_model_metadata", lambda *a, **kw: {})
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kw: {"api_mode": "chat_completions"},
    )
    (get_hermes_home() / "config.yaml").write_text(
        json.dumps(
            {
                "auxiliary": {
                    "transient_retries": 1,
                    "approval": {
                        "provider": "zai",
                        "model": "glm-5.3",
                        "fallback_chain": [
                            {"provider": "custom:omlx", "model": "Qwen3.8-35B"}
                        ],
                    },
                },
                # Shared endpoint: the chain entry's own label must survive.
                "custom_providers": [
                    {"name": "omlx", "base_url": OMLX},
                    {"name": "qwen36-mlx", "base_url": OMLX},
                ],
            }
        ),
        encoding="utf-8",
    )
    aux._client_cache.clear()
    yield script, sent, accounted, monkeypatch
    aux._client_cache.clear()


def _capture(wiring):
    _, _, accounted, monkeypatch = wiring
    monkeypatch.setattr(
        "agent.aux_accounting.record_aux_usage",
        lambda response, task, **kw: accounted.append(dict(kw, model=response.model)),
    )


def invoke(async_mode, **kwargs):
    args = dict(
        task="approval",
        messages=[{"role": "user", "content": "approve fixture command?"}],
        main_runtime={
            "provider": "zai",
            "model": "glm-5.3",
            "base_url": ZAI,
            "api_key": "synthetic-fixture",
            "api_mode": "chat_completions",
        },
    )
    args.update(kwargs)
    if async_mode:
        return asyncio.run(aux.async_call_llm(**args))
    return aux.call_llm(**args)


RETRIES = {
    "transient": (dict(), ConnectionFixtureError("connection reset by fixture")),
    "temperature": (
        dict(temperature=0.3),
        Exception("Unsupported parameter: temperature is not supported"),
    ),
    "max_tokens": (
        dict(max_tokens=64),
        Exception("unsupported_parameter: max_tokens"),
    ),
}


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("kind", sorted(RETRIES))
def test_same_provider_retry_records_its_route(wiring, async_mode, kind):
    script, sent, accounted, _ = wiring
    _capture(wiring)
    kwargs, error = RETRIES[kind]
    script["zai"] = [error]

    invoke(async_mode, **kwargs)

    assert [s[0] for s in sent] == ["zai", "zai"], "fixture did not take the retry path"
    assert accounted == [{"provider": "zai", "base_url": ZAI, "model": "glm-5.3"}]


@pytest.mark.parametrize("async_mode", [False, True])
def test_fallback_hop_records_the_chain_entry(wiring, async_mode):
    script, sent, accounted, _ = wiring
    _capture(wiring)
    script["zai"] = [RateLimitError("429 rate limit, try again")] * 4

    invoke(async_mode)

    assert sent[-1][0] == "custom:omlx", "fixture did not reach the chain entry"
    assert accounted == [
        {"provider": "custom:omlx", "base_url": OMLX, "model": "Qwen3.8-35B"}
    ]


@pytest.mark.parametrize("async_mode", [False, True])
def test_first_attempt_unchanged(wiring, async_mode):
    _, sent, accounted, _ = wiring
    _capture(wiring)

    invoke(async_mode)

    assert len(sent) == 1
    assert accounted == [{"provider": "zai", "base_url": ZAI, "model": "glm-5.3"}]


def test_fallback_hop_lands_in_both_stores(wiring, tmp_path):
    script, sent, _, _ = wiring
    events._ledgers.clear()
    db = SessionDB(tmp_path / "state.db")
    db.create_session("aux-route", source="discord", model="glm-5.3")
    token = set_accounting_context(db, "aux-route")
    try:
        script["zai"] = [RateLimitError("429 rate limit, try again")] * 4
        invoke(False)
    finally:
        reset_accounting_context(token)
    try:
        assert sent[-1][0] == "custom:omlx"
        rows = db._conn.execute(
            "SELECT task, billing_provider, model, input_tokens, output_tokens FROM session_model_usage"
        ).fetchall()
        assert [tuple(r) for r in rows] == [("approval", "custom:omlx", "Qwen3.8-35B", 10, 2)]
        path = get_hermes_home() / events.SIDECAR_FILENAME
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            assert conn.execute(
                "SELECT purpose, provider, billing_base_url FROM usage_events"
            ).fetchall() == [("aux:approval", "custom:omlx", OMLX)]
    finally:
        db.close()
        for ledger in list(events._ledgers.values()):
            ledger.close()
        events._ledgers.clear()


def test_every_recording_site_passes_a_route():
    """Structural guard: no _validate_llm_response call in the aux client may
    omit provider/base_url again (a new retry rung would silently reintroduce
    empty-route rows)."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(aux))
    missing = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_validate_llm_response":
            names = {k.arg for k in node.keywords}
            if not {"provider", "base_url"} <= names and len(node.args) < 4:
                missing.append(node.lineno)
    assert not missing, f"_validate_llm_response calls without a route at lines {missing}"
