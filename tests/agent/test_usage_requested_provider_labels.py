"""A requested named provider disambiguates a shared custom endpoint.

Keeper, Discord #gateway msg 1555093184839942206 (2026-10-01): "yeah make the
change then. the one that doesn't change the schema." The change answers the
2026-09-30 coverage gap where four oMLX calls were recorded as bare ``custom``:
the agent knew it had been asked for ``custom:omlx``, but the accounting
boundary only reverse-mapped the base URL, and four configured providers share
that URL. The requested name now reaches the qualifier and is written into the
existing provider columns of both stores; no column is added.

Guard: a requested name is used only when its configured endpoint is the
endpoint the call actually hit, so a call that fell back elsewhere is never
written under the name it was originally asked for.
"""

import json
import sqlite3
import threading

import pytest

import agent.usage_events as events
from hermes_constants import get_hermes_home
from hermes_state import SessionDB

OMLX = "https://omlx.acubens.pharos.zone/v1"
DS4 = "https://ds4.acubens.pharos.zone/v1"

SHARED = {
    "custom_providers": [
        {"name": "gemma-4-moe-it", "base_url": OMLX},
        {"name": "qwen36-mlx", "base_url": OMLX},
        {"name": "omlx", "base_url": OMLX},
        {"name": "ds4-local", "base_url": DS4},
    ]
}


@pytest.fixture
def store(tmp_path):
    events._ledgers.clear()
    db = SessionDB(tmp_path / "state.db")
    db.create_session("requested-route", source="cli", model="Qwen3.8-35B")
    yield db
    db.close()
    for ledger in list(events._ledgers.values()):
        ledger.close()
    events._ledgers.clear()


def config(value):
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(json.dumps(value), encoding="utf-8")


KWARGS = dict(
    model="Qwen3.8-35B",
    input_tokens=7,
    output_tokens=3,
    cache_read_tokens=11,
    cache_write_tokens=5,
)


def record(db, path, *, requested, provider="custom", url=OMLX):
    kwargs = dict(KWARGS, billing_provider=provider, billing_base_url=url)
    if requested is not None:
        kwargs["billing_requested_provider"] = requested
    if path == "aux":
        db.record_auxiliary_usage("requested-route", "background_review", **kwargs)
    elif path == "queued":
        db.queue_token_counts("requested-route", api_call_count=1, **kwargs)
        assert db.flush_token_counts(5)
    else:
        db.update_token_counts("requested-route", api_call_count=1, **kwargs)


def assert_stores(db, expected):
    rows = db._conn.execute(
        "SELECT billing_provider, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens FROM session_model_usage"
    ).fetchall()
    assert [tuple(r) for r in rows] == [(expected, 7, 3, 11, 5)]
    path = get_hermes_home() / events.SIDECAR_FILENAME
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        assert conn.execute(
            "SELECT provider, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens FROM usage_events"
        ).fetchall() == [(expected, 7, 3, 11, 5)]


PATHS = ["direct", "queued", "aux"]


@pytest.mark.parametrize("path", PATHS)
def test_requested_name_resolves_shared_endpoint(store, path):
    config(SHARED)
    record(store, path, requested="custom:omlx")
    assert_stores(store, "custom:omlx")


@pytest.mark.parametrize("path", PATHS)
def test_shared_endpoint_without_requested_name_stays_bare(store, path):
    """Unchanged behaviour: the endpoint alone is ambiguous."""
    config(SHARED)
    record(store, path, requested=None)
    assert_stores(store, "custom")


@pytest.mark.parametrize("path", PATHS)
def test_requested_name_for_another_endpoint_is_not_used(store, path):
    """A call that landed on ds4 is never written as the omlx it was asked for;
    the unique ds4 endpoint names itself instead."""
    config(SHARED)
    record(store, path, requested="custom:omlx", url=DS4)
    assert_stores(store, "custom:ds4-local")


@pytest.mark.parametrize("path", PATHS)
def test_requested_name_on_unconfigured_endpoint_stays_bare(store, path):
    config(SHARED)
    record(store, path, requested="custom:omlx", url="http://127.0.0.1:9/v1")
    assert_stores(store, "custom")


@pytest.mark.parametrize("requested", ["custom", "custom:unknown", "", "openai-codex"])
def test_unmatched_requested_name_falls_back_to_endpoint_rule(store, requested):
    config(SHARED)
    record(store, "direct", requested=requested)
    assert_stores(store, "custom")


def test_requested_name_matches_keyed_provider(store):
    config(
        {
            "providers": {
                "omlx": {"name": "Acubens oMLX", "base_url": OMLX},
                "qwen36-mlx": {"name": "Qwen 3.6", "base_url": OMLX},
            }
        }
    )
    record(store, "direct", requested="custom:omlx")
    assert_stores(store, "custom:omlx")


def test_qualified_label_is_not_reinterpreted_by_requested_name(store):
    config(SHARED)
    record(store, "direct", requested="custom:omlx", provider="custom:qwen36-mlx")
    assert_stores(store, "custom:qwen36-mlx")


def test_requested_name_never_reaches_the_queue(store, monkeypatch):
    """Resolved before enqueue in the caller's profile; queued deltas carry
    the resolved label only, so coalescing never sees the request field."""
    config(SHARED)
    seen = []
    started, release = threading.Event(), threading.Event()
    original = store._apply_token_batch

    def delayed(batch):
        seen.extend(batch)
        started.set()
        assert release.wait(5)
        original(batch)

    monkeypatch.setattr(store, "_apply_token_batch", delayed)
    try:
        store.queue_token_counts(
            "requested-route",
            api_call_count=1,
            billing_provider="custom",
            billing_base_url=OMLX,
            billing_requested_provider="custom:omlx",
            **KWARGS,
        )
        assert started.wait(5)
    finally:
        release.set()
    assert store.flush_token_counts(5)
    assert seen, "writer received no batch"
    for item in seen:
        kwargs = item[1] if isinstance(item, tuple) else item
        assert "billing_requested_provider" not in kwargs
        assert kwargs["billing_provider"] == "custom:omlx"
    assert_stores(store, "custom:omlx")
