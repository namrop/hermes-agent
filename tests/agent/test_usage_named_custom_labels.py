"""Named custom routes stay identifiable in both real usage stores."""

import json
import sqlite3
import threading

import pytest

import agent.usage_events as events
from hermes_constants import (
    get_hermes_home,
    set_hermes_home_override,
    reset_hermes_home_override,
)
from hermes_state import SessionDB

URL = "http://127.0.0.1:3458"


@pytest.fixture
def store(tmp_path):
    events._ledgers.clear()
    db = SessionDB(tmp_path / "state.db")
    db.create_session("named-route", source="cron", model="claude-opus-5-5")
    yield db
    db.close()
    for ledger in list(events._ledgers.values()):
        ledger.close()
    events._ledgers.clear()


def config(value, home=None):
    home = home or get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(json.dumps(value), encoding="utf-8")


def record(db, task="", provider="custom", url=URL):
    kwargs = dict(
        model="claude-opus-5-5",
        billing_provider=provider,
        billing_base_url=url,
        input_tokens=7,
        output_tokens=3,
        cache_read_tokens=11,
        cache_write_tokens=5,
    )
    if task:
        db.record_auxiliary_usage("named-route", task, **kwargs)
    else:
        db.update_token_counts("named-route", api_call_count=1, **kwargs)


def assert_stores(db, expected):
    rows = db._conn.execute(
        "SELECT billing_provider, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens FROM session_model_usage"
    ).fetchall()
    assert len(rows) == 1
    assert tuple(rows[0]) == (expected, 7, 3, 11, 5)
    path = get_hermes_home() / events.SIDECAR_FILENAME
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        assert conn.execute(
            "SELECT provider, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens FROM usage_events"
        ).fetchall() == [(expected, 7, 3, 11, 5)]


@pytest.mark.parametrize("task", ["", "vision", "background_review"])
@pytest.mark.parametrize("style", ["legacy", "keyed"])
def test_qualifies_unique_endpoint_in_both_stores(store, task, style):
    cfg = (
        {"custom_providers": [{"name": "Meridian Primary", "base_url": URL}]}
        if style == "legacy"
        else {
            "providers": {"meridian-primary": {"name": "Display name", "base_url": URL}}
        }
    )
    config(cfg)
    record(store, task)
    assert_stores(store, "custom:meridian-primary")
    if not task:
        assert (
            store.get_session("named-route")["billing_provider"]
            == "custom:meridian-primary"
        )


@pytest.mark.parametrize(
    "cfg,url",
    [
        ({}, URL),
        (
            {
                "custom_providers": [
                    {"name": "one", "base_url": URL},
                    {"name": "two", "base_url": URL},
                ]
            },
            URL,
        ),
        (
            {
                "providers": {
                    "one": {"name": "Same display", "base_url": URL},
                    "two": {"name": "Same display", "base_url": URL},
                }
            },
            URL,
        ),
        ({"custom_providers": [{"name": "one", "base_url": URL + "/A"}]}, URL + "/a"),
        (
            {"custom_providers": [{"name": "one", "base_url": URL + "?account=one"}]},
            URL + "?account=two",
        ),
    ],
)
def test_unresolved_and_ambiguous_remain_bare(store, cfg, url):
    config(cfg)
    record(store, url=url)
    assert_stores(store, "custom")


def test_qualified_label_is_not_reinterpreted(store):
    config({"custom_providers": [{"name": "other", "base_url": URL}]})
    record(store, provider="custom:original")
    assert_stores(store, "custom:original")


def test_lookup_failure_does_not_drop_usage(store, monkeypatch):
    def broken():
        raise OSError("fixture unreadable config")

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", broken)
    record(store)
    assert_stores(store, "custom")


def test_profile_context_selects_its_own_name(store, tmp_path):
    config({"custom_providers": [{"name": "default", "base_url": URL}]})
    scoped = tmp_path / "scoped-home"
    config({"custom_providers": [{"name": "scoped", "base_url": URL}]}, scoped)
    token = set_hermes_home_override(scoped)
    try:
        record(store)
        assert_stores(store, "custom:scoped")
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize(
    "scoped_entries,expected",
    [
        ([{"name": "scoped", "base_url": URL}], "custom:scoped"),
        ([], "custom"),
    ],
)
def test_queued_label_is_resolved_before_profile_scope_exits(
    store, tmp_path, monkeypatch, scoped_entries, expected
):
    config({"custom_providers": [{"name": "default", "base_url": URL}]})
    scoped = tmp_path / "queue-home"
    config({"custom_providers": scoped_entries}, scoped)
    started, release = threading.Event(), threading.Event()
    original = store._apply_token_batch

    def delayed(batch):
        started.set()
        assert release.wait(5)
        original(batch)

    monkeypatch.setattr(store, "_apply_token_batch", delayed)
    token = set_hermes_home_override(scoped)
    try:
        store.queue_token_counts(
            "named-route",
            model="claude-opus-5-5",
            billing_provider="custom",
            billing_base_url=URL,
            api_call_count=1,
            input_tokens=7,
            output_tokens=3,
            cache_read_tokens=11,
            cache_write_tokens=5,
        )
        assert started.wait(5)
    finally:
        reset_hermes_home_override(token)
        release.set()
    assert store.flush_token_counts(5)
    assert_stores(store, expected)
