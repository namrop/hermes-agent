"""Contract tests for canonical, event-derived usage read models.

Every summary row has the keys in ``SUMMARY_KEYS``. Provider/model rows add
``provider`` and ``model``; daily rows add ``date``; session-route rows add
``provider``, ``model``, and ``purpose``. Costs are unrounded USD floats and
NULL provider/model values remain NULL so unattributed usage is visible.
"""

from datetime import datetime, timezone

import pytest

from hermes_state import SessionDB


SUMMARY_KEYS = {
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "prompt_tokens",
    "total_tokens",
    "event_count",
    "api_attempt_count",
    "successful_call_count",
    "latency_sample_count",
    "latency_total_ms",
    "average_latency_ms",
    "historical_aggregate_count",
    "reconstructed_call_count",
    "estimated_cost_usd",
    "actual_cost_usd",
    "estimated_cost_known_event_count",
    "estimated_cost_unknown_event_count",
    "actual_cost_known_event_count",
    "actual_cost_unknown_event_count",
}

ADDITIVE_SUMMARY_KEYS = SUMMARY_KEYS - {"average_latency_ms"}


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "usage-analytics.db")
    yield session_db
    session_db.close()


def _record(db, uid, **overrides):
    values = {
        "session_id": "session-1",
        "timestamp": 1_700_000_000.0,
        "source": "discord",
        "purpose": "main",
        "provider": "openrouter",
        "model": "shared-model",
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 3,
        "cache_write_tokens": 2,
        "reasoning_tokens": 4,
        "estimated_cost_usd": 0.25,
        "actual_cost_usd": 0.2,
        "latency_ms": 100,
        "request_status": "ok",
        "api_call_index": 1,
    }
    values.update(overrides)
    return db.record_usage_and_rollup(event_uid=uid, **values)


def _update_event(db, uid, **values):
    assignments = ", ".join(f"{column} = ?" for column in values)
    params = (*values.values(), uid)
    db._execute_write(
        lambda conn: conn.execute(
            f"UPDATE llm_usage_events SET {assignments} WHERE event_uid = ?", params
        )
    )


def _epoch(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


def test_mixed_session_provider_model_rows_reconcile_to_global_totals(db):
    _record(db, "one", provider="openrouter", model="model-a")
    _record(
        db,
        "two",
        provider="deepseek",
        model="model-b",
        input_tokens=20,
        output_tokens=7,
        cache_read_tokens=6,
        cache_write_tokens=1,
        reasoning_tokens=2,
        estimated_cost_usd=0.4,
        actual_cost_usd=None,
        latency_ms=300,
    )

    total = db.summarize_usage_events(session_id="session-1")
    rows = db.summarize_usage_by_provider_model(session_id="session-1")

    assert set(total) == SUMMARY_KEYS
    assert {(row["provider"], row["model"]) for row in rows} == {
        ("openrouter", "model-a"),
        ("deepseek", "model-b"),
    }
    assert all(set(row) == SUMMARY_KEYS | {"provider", "model"} for row in rows)
    for key in ADDITIVE_SUMMARY_KEYS:
        assert sum(row[key] for row in rows) == pytest.approx(total[key])
    assert total["average_latency_ms"] == pytest.approx(200.0)


def test_same_model_name_across_providers_stays_separate(db):
    _record(db, "router", provider="openrouter", model="same-name")
    _record(db, "direct", provider="deepseek", model="same-name")

    rows = db.summarize_usage_by_provider_model()

    assert [(row["provider"], row["model"]) for row in rows] == [
        ("deepseek", "same-name"),
        ("openrouter", "same-name"),
    ]


def test_daily_grouping_uses_event_timestamp_and_cutoff_not_session_start(db):
    db.create_session(session_id="old-session", source="discord")
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (_epoch(2020, 1, 1), "old-session"),
        )
    )
    _record(
        db,
        "before-cutoff",
        session_id="old-session",
        timestamp=_epoch(2026, 7, 11, 10),
    )
    _record(
        db,
        "after-cutoff",
        session_id="old-session",
        timestamp=_epoch(2026, 7, 12, 2),
        input_tokens=99,
    )

    rows = db.summarize_usage_daily(
        cutoff=_epoch(2026, 7, 11, 12), timezone_name="UTC"
    )

    assert len(rows) == 1
    assert rows[0]["date"] == "2026-07-12"
    assert rows[0]["input_tokens"] == 99
    assert set(rows[0]) == SUMMARY_KEYS | {"date"}


def test_historical_aggregate_contributes_usage_not_attempt_metrics(db):
    _record(db, "real", input_tokens=7, latency_ms=50)
    _record(
        db,
        "historical",
        input_tokens=70,
        output_tokens=20,
        estimated_cost_usd=1.5,
        actual_cost_usd=1.25,
        latency_ms=999,
        api_call_index=7,
    )
    _update_event(
        db,
        "historical",
        record_kind="historical_aggregate",
        request_status="approximate_session_backfill",
    )

    summary = db.summarize_usage_events()

    assert summary["event_count"] == 2
    assert summary["input_tokens"] == 77
    assert summary["estimated_cost_usd"] == pytest.approx(1.75)
    assert summary["actual_cost_usd"] == pytest.approx(1.45)
    assert summary["api_attempt_count"] == 1
    assert summary["successful_call_count"] == 1
    assert summary["latency_sample_count"] == 1
    assert summary["latency_total_ms"] == 50
    assert summary["average_latency_ms"] == pytest.approx(50.0)
    assert summary["historical_aggregate_count"] == 1
    assert summary["reconstructed_call_count"] == 7


def test_actual_and_estimated_cost_coverage_are_independent(db):
    _record(db, "both", estimated_cost_usd=0.1, actual_cost_usd=0.2)
    _record(db, "estimated", estimated_cost_usd=0.3, actual_cost_usd=None)
    _record(db, "actual", estimated_cost_usd=None, actual_cost_usd=0.4)
    _record(db, "neither", estimated_cost_usd=None, actual_cost_usd=None)

    summary = db.summarize_usage_events()

    assert summary["estimated_cost_usd"] == pytest.approx(0.4)
    assert summary["actual_cost_usd"] == pytest.approx(0.6)
    assert summary["estimated_cost_known_event_count"] == 2
    assert summary["estimated_cost_unknown_event_count"] == 2
    assert summary["actual_cost_known_event_count"] == 2
    assert summary["actual_cost_unknown_event_count"] == 2


def test_reasoning_tokens_are_annotation_not_added_to_total(db):
    _record(
        db,
        "reasoning",
        input_tokens=10,
        cache_read_tokens=3,
        cache_write_tokens=2,
        output_tokens=7,
        reasoning_tokens=6,
    )

    summary = db.summarize_usage_events()

    assert summary["prompt_tokens"] == 15
    assert summary["total_tokens"] == 22
    assert summary["reasoning_tokens"] == 6


def test_source_purpose_session_and_cutoff_filters_apply_to_event_rows(db):
    _record(db, "discord-main", timestamp=100, source="discord", purpose="main")
    _record(
        db,
        "discord-review",
        timestamp=200,
        source="discord",
        purpose="background_review",
    )
    _record(
        db,
        "cli-main",
        session_id="session-2",
        timestamp=300,
        source="cli",
        purpose="main",
    )

    assert db.summarize_usage_events(source="discord")["event_count"] == 2
    assert db.summarize_usage_events(purpose="main")["event_count"] == 2
    assert db.summarize_usage_events(
        cutoff=150, source="discord", purpose="background_review"
    )["event_count"] == 1
    assert db.summarize_usage_events(session_id="session-2")["event_count"] == 1
    events = db.query_llm_usage_events(
        cutoff=150, source="discord", purpose="background_review"
    )
    assert [event["event_uid"] for event in events] == ["discord-review"]


def test_filtered_event_retrieval_has_no_display_limit(db):
    db.create_session(session_id="bulk-session", source="bulk")
    rows = [
        (
            float(index),
            "bulk-session",
            "bulk",
            "main",
            index,
            float(index),
        )
        for index in range(10_005)
    ]
    db._execute_write(
        lambda conn: conn.executemany(
            """INSERT INTO llm_usage_events
               (timestamp, session_id, source, purpose, input_tokens, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )
    )

    events = db.query_llm_usage_events(
        cutoff=2, source="bulk", session_id="bulk-session", purpose="main"
    )

    assert len(events) == 10_003
    assert events[0]["timestamp"] == 2.0
    assert events[-1]["timestamp"] == 10_004.0


def test_unattributed_provider_model_is_an_explicit_group(db):
    _record(db, "known")
    _record(db, "unknown", provider=None, model=None, input_tokens=40)

    rows = db.summarize_usage_by_provider_model()

    unattributed = next(
        row for row in rows if row["provider"] is None and row["model"] is None
    )
    assert unattributed["event_count"] == 1
    assert unattributed["input_tokens"] == 40
    assert sum(row["input_tokens"] for row in rows) == 50


def test_legacy_null_record_kind_is_an_api_attempt(db):
    _record(db, "legacy", request_status="ok", latency_ms=75)
    _update_event(db, "legacy", record_kind=None)
    _record(db, "failed", request_status="error", latency_ms=25)
    _record(db, "non-attempt", request_status="ok", latency_ms=999)
    _update_event(db, "non-attempt", record_kind="correction")

    summary = db.summarize_usage_events()

    assert summary["api_attempt_count"] == 2
    assert summary["successful_call_count"] == 1
    assert summary["latency_sample_count"] == 2
    assert summary["latency_total_ms"] == 100
    assert summary["historical_aggregate_count"] == 0


def test_explicit_timezone_groups_events_around_local_midnight(db):
    _record(db, "early", timestamp=_epoch(2026, 7, 12, 0, 30))
    _record(db, "late", timestamp=_epoch(2026, 7, 12, 7, 30))

    utc_rows = db.summarize_usage_daily(timezone_name="UTC")
    los_angeles_rows = db.summarize_usage_daily(
        timezone_name="America/Los_Angeles"
    )

    assert [row["date"] for row in utc_rows] == ["2026-07-12"]
    assert [row["event_count"] for row in utc_rows] == [2]
    assert [row["date"] for row in los_angeles_rows] == [
        "2026-07-11",
        "2026-07-12",
    ]
    assert [row["event_count"] for row in los_angeles_rows] == [1, 1]


def test_invalid_timezone_name_raises_clear_value_error(db):
    with pytest.raises(ValueError, match="Invalid timezone_name: Not/A_Zone"):
        db.summarize_usage_daily(timezone_name="Not/A_Zone")


def test_session_routes_group_by_provider_model_and_purpose(db):
    _record(db, "main", provider="openrouter", model="same", purpose="main")
    _record(
        db,
        "review",
        provider="openrouter",
        model="same",
        purpose="background_review",
    )
    _record(db, "direct", provider="deepseek", model="same", purpose="main")
    _record(db, "none", provider=None, model=None, purpose="main")
    _record(db, "other", session_id="session-2", provider="ignored")

    rows = db.summarize_session_routes("session-1")

    assert {
        (row["provider"], row["model"], row["purpose"]) for row in rows
    } == {
        ("openrouter", "same", "main"),
        ("openrouter", "same", "background_review"),
        ("deepseek", "same", "main"),
        (None, None, "main"),
    }
    assert all(
        set(row) == SUMMARY_KEYS | {"provider", "model", "purpose"}
        for row in rows
    )
    assert sum(row["event_count"] for row in rows) == 4
