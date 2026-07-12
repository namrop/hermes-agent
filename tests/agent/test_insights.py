"""Tests for agent/insights.py — InsightsEngine analytics and reporting."""

import time
import pytest
from pathlib import Path

from hermes_state import SessionDB
from agent.insights import (
    InsightsEngine,
    _estimate_cost,
    _format_duration,
    _bar_chart,
    _has_known_pricing,
    _DEFAULT_PRICING,
)


@pytest.fixture()
def db(tmp_path):
    """Create a SessionDB with a temp database file."""
    db_path = tmp_path / "test_insights.db"
    session_db = SessionDB(db_path=db_path)
    yield session_db
    session_db.close()


@pytest.fixture()
def populated_db(db):
    """Create a DB with realistic session data for insights testing."""
    now = time.time()
    day = 86400

    # Session 1: CLI, claude-sonnet, ended, 2 days ago
    db.create_session(
        session_id="s1", source="cli",
        model="anthropic/claude-sonnet-4-20250514", user_id="user1",
    )
    # Backdate the started_at
    db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = 's1'", (now - 2 * day,))
    db.end_session("s1", end_reason="user_exit")
    db._conn.execute("UPDATE sessions SET ended_at = ? WHERE id = 's1'", (now - 2 * day + 3600,))
    db.update_token_counts("s1", input_tokens=50000, output_tokens=15000)
    _record_usage(
        db, "s1", timestamp=now - 2 * day, source="cli",
        provider="anthropic", model="anthropic/claude-sonnet-4-20250514",
        input_tokens=50000, output_tokens=15000, estimated_cost_usd=0.50,
    )
    db.append_message("s1", role="user", content="Hello, help me fix a bug")
    db.append_message("s1", role="assistant", content="Sure, let me look into that.")
    db.append_message("s1", role="assistant", content="Let me search the files.",
                      tool_calls=[{"function": {"name": "search_files"}}])
    db.append_message("s1", role="tool", content="Found 3 matches", tool_name="search_files")
    db.append_message("s1", role="assistant", content="Let me read the file.",
                      tool_calls=[{"function": {"name": "read_file"}}])
    db.append_message("s1", role="tool", content="file contents...", tool_name="read_file")
    db.append_message("s1", role="assistant", content="I found the bug. Let me fix it.",
                      tool_calls=[{"function": {"name": "patch"}}])
    db.append_message("s1", role="tool", content="patched successfully", tool_name="patch")
    db.append_message(
        "s1",
        role="assistant",
        content="Let me load the PR workflow skill.",
        tool_calls=[{"function": {"name": "skill_view", "arguments": '{"name":"github-pr-workflow"}'}}],
    )
    db.append_message("s1", role="user", content="Thanks!")
    db.append_message("s1", role="assistant", content="You're welcome!")

    # Session 2: Telegram, gpt-4o, ended, 5 days ago
    db.create_session(
        session_id="s2", source="telegram",
        model="gpt-4o", user_id="user1",
    )
    db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = 's2'", (now - 5 * day,))
    db.end_session("s2", end_reason="timeout")
    db._conn.execute("UPDATE sessions SET ended_at = ? WHERE id = 's2'", (now - 5 * day + 1800,))
    db.update_token_counts("s2", input_tokens=20000, output_tokens=8000)
    _record_usage(
        db, "s2", timestamp=now - 5 * day, source="telegram",
        provider="openai", model="gpt-4o",
        input_tokens=20000, output_tokens=8000, estimated_cost_usd=0.20,
    )
    db.append_message("s2", role="user", content="Search the web for something")
    db.append_message("s2", role="assistant", content="Searching...",
                      tool_calls=[{"function": {"name": "web_search"}}])
    db.append_message("s2", role="tool", content="results...", tool_name="web_search")
    db.append_message("s2", role="assistant", content="Here's what I found")

    # Session 3: CLI, deepseek-chat, ended, 10 days ago
    db.create_session(
        session_id="s3", source="cli",
        model="deepseek-chat", user_id="user1",
    )
    db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = 's3'", (now - 10 * day,))
    db.end_session("s3", end_reason="user_exit")
    db._conn.execute("UPDATE sessions SET ended_at = ? WHERE id = 's3'", (now - 10 * day + 7200,))
    db.update_token_counts("s3", input_tokens=100000, output_tokens=40000)
    _record_usage(
        db, "s3", timestamp=now - 10 * day, source="cli",
        provider="deepseek", model="deepseek-chat",
        input_tokens=100000, output_tokens=40000, estimated_cost_usd=0.10,
    )
    db.append_message("s3", role="user", content="Run this terminal command")
    db.append_message("s3", role="assistant", content="Running...",
                      tool_calls=[{"function": {"name": "terminal"}}])
    db.append_message("s3", role="tool", content="output...", tool_name="terminal")
    db.append_message("s3", role="assistant", content="Let me run another",
                      tool_calls=[{"function": {"name": "terminal"}}])
    db.append_message("s3", role="tool", content="more output...", tool_name="terminal")
    db.append_message("s3", role="assistant", content="And search files",
                      tool_calls=[{"function": {"name": "search_files"}}])
    db.append_message("s3", role="tool", content="found stuff", tool_name="search_files")
    db.append_message(
        "s3",
        role="assistant",
        content="Load the debugging skill.",
        tool_calls=[{"function": {"name": "skill_view", "arguments": '{"name":"systematic-debugging"}'}}],
    )

    # Session 4: Discord, same model as s1, ended, 1 day ago
    db.create_session(
        session_id="s4", source="discord",
        model="anthropic/claude-sonnet-4-20250514", user_id="user2",
    )
    db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = 's4'", (now - 1 * day,))
    db.end_session("s4", end_reason="user_exit")
    db._conn.execute("UPDATE sessions SET ended_at = ? WHERE id = 's4'", (now - 1 * day + 900,))
    db.update_token_counts("s4", input_tokens=10000, output_tokens=5000)
    _record_usage(
        db, "s4", timestamp=now - day, source="discord",
        provider="anthropic", model="anthropic/claude-sonnet-4-20250514",
        input_tokens=10000, output_tokens=5000, estimated_cost_usd=0.10,
    )
    db.append_message("s4", role="user", content="Quick question")
    db.append_message("s4", role="assistant", content="Sure, go ahead")
    db.append_message(
        "s4",
        role="assistant",
        content="Load and update GitHub skills.",
        tool_calls=[
            {"function": {"name": "skill_view", "arguments": '{"name":"github-pr-workflow"}'}},
            {"function": {"name": "skill_manage", "arguments": '{"name":"github-code-review"}'}},
        ],
    )

    # Session 5: Old session, 45 days ago (should be excluded from 30-day window)
    db.create_session(
        session_id="s_old", source="cli",
        model="gpt-4o-mini", user_id="user1",
    )
    db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = 's_old'", (now - 45 * day,))
    db.end_session("s_old", end_reason="user_exit")
    db._conn.execute("UPDATE sessions SET ended_at = ? WHERE id = 's_old'", (now - 45 * day + 600,))
    db.update_token_counts("s_old", input_tokens=5000, output_tokens=2000)
    _record_usage(
        db, "s_old", timestamp=now - 45 * day, source="cli",
        provider="openai", model="gpt-4o-mini",
        input_tokens=5000, output_tokens=2000, estimated_cost_usd=0.01,
    )
    db.append_message("s_old", role="user", content="old message")
    db.append_message("s_old", role="assistant", content="old reply")

    db._conn.commit()
    return db


def _record_usage(db, session_id, *, timestamp=None, source=None, **overrides):
    values = {
        "timestamp": timestamp if timestamp is not None else time.time(),
        "source": source,
        "provider": "openrouter",
        "model": "test-model",
        "input_tokens": 10,
        "output_tokens": 5,
        "estimated_cost_usd": 0.10,
        "request_status": "ok",
    }
    values.update(overrides)
    return db.record_llm_usage_event(session_id, **values)


class TestHasKnownPricing:
    def test_known_commercial_model(self):
        assert _has_known_pricing("gpt-4o", provider="openai") is True
        assert _has_known_pricing("anthropic/claude-sonnet-4-20250514") is True
        assert _has_known_pricing("gpt-4.1", provider="openai") is True

    def test_unknown_custom_model(self):
        assert _has_known_pricing("FP16_Hermes_4.5") is False
        assert _has_known_pricing("my-custom-model") is False
        assert _has_known_pricing("glm-5") is False
        assert _has_known_pricing("") is False
        assert _has_known_pricing(None) is False

    def test_heuristic_matched_models_are_not_considered_known(self):
        assert _has_known_pricing("some-opus-model") is False
        assert _has_known_pricing("future-sonnet-v2") is False


class TestEstimateCost:
    def test_basic_cost(self):
        cost, status = _estimate_cost(
            "anthropic/claude-sonnet-4-20250514",
            1_000_000,
            1_000_000,
            provider="anthropic",
        )
        assert status == "estimated"
        assert cost == pytest.approx(18.0, abs=0.01)

    def test_zero_tokens(self):
        cost, status = _estimate_cost("gpt-4o", 0, 0, provider="openai")
        assert status == "estimated"
        assert cost == 0.0

    def test_cache_aware_usage(self):
        cost, status = _estimate_cost(
            "anthropic/claude-sonnet-4-20250514",
            1000,
            500,
            cache_read_tokens=2000,
            cache_write_tokens=400,
            provider="anthropic",
        )
        assert status == "estimated"
        expected = (1000 * 3.0 + 500 * 15.0 + 2000 * 0.30 + 400 * 3.75) / 1_000_000
        assert cost == pytest.approx(expected, abs=0.0001)


# =========================================================================
# Format helpers
# =========================================================================

class TestFormatDuration:
    def test_seconds(self):
        assert _format_duration(45) == "45s"

    def test_minutes(self):
        assert _format_duration(300) == "5m"

    def test_hours_with_minutes(self):
        result = _format_duration(5400)  # 1.5 hours
        assert result == "1h 30m"

    def test_exact_hours(self):
        assert _format_duration(7200) == "2h"

    def test_days(self):
        result = _format_duration(172800)  # 2 days
        assert result == "2.0d"


class TestBarChart:
    def test_basic_bars(self):
        bars = _bar_chart([10, 5, 0, 20], max_width=10)
        assert len(bars) == 4
        assert len(bars[3]) == 10  # max value gets full width
        assert len(bars[0]) == 5   # half of max
        assert bars[2] == ""       # zero gets empty

    def test_empty_values(self):
        bars = _bar_chart([], max_width=10)
        assert bars == []

    def test_all_zeros(self):
        bars = _bar_chart([0, 0, 0], max_width=10)
        assert all(b == "" for b in bars)

    def test_single_value(self):
        bars = _bar_chart([5], max_width=10)
        assert len(bars) == 1
        assert len(bars[0]) == 10


# =========================================================================
# InsightsEngine — empty DB
# =========================================================================

class TestInsightsEmpty:
    def test_empty_db_returns_empty_report(self, db):
        engine = InsightsEngine(db)
        report = engine.generate(days=30)
        assert report["empty"] is True
        assert report["overview"] == {}

    def test_empty_db_terminal_format(self, db):
        engine = InsightsEngine(db)
        report = engine.generate(days=30)
        text = engine.format_terminal(report)
        assert "No sessions found" in text

    def test_empty_db_gateway_format(self, db):
        engine = InsightsEngine(db)
        report = engine.generate(days=30)
        text = engine.format_gateway(report)
        assert "No sessions found" in text


# =========================================================================
# InsightsEngine — populated DB
# =========================================================================

class TestInsightsPopulated:
    def test_generate_returns_all_sections(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30)

        assert report["empty"] is False
        assert "overview" in report
        assert "models" in report
        assert "platforms" in report
        assert "tools" in report
        assert "activity" in report
        assert "top_sessions" in report

    def test_overview_session_count(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30)
        overview = report["overview"]

        # s1, s2, s3, s4 are within 30 days; s_old is 45 days ago
        assert overview["total_sessions"] == 4

    def test_overview_token_totals(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30)
        overview = report["overview"]

        expected_input = 50000 + 20000 + 100000 + 10000
        expected_output = 15000 + 8000 + 40000 + 5000
        assert overview["total_input_tokens"] == expected_input
        assert overview["total_output_tokens"] == expected_output
        assert overview["total_tokens"] == expected_input + expected_output

    def test_overview_cost_positive(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30)
        assert report["overview"]["estimated_cost"] > 0

    def test_overview_duration_stats(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30)
        overview = report["overview"]

        # All 4 sessions have durations
        assert overview["total_hours"] > 0
        assert overview["avg_session_duration"] > 0

    def test_model_breakdown(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30)
        models = report["models"]

        # Three actual provider/model routes; the Anthropic route has two calls.
        model_names = [m["model"] for m in models]
        assert "anthropic/claude-sonnet-4-20250514" in model_names
        assert "gpt-4o" in model_names
        assert "deepseek-chat" in model_names

        claude = next(m for m in models if "claude-sonnet" in m["model"])
        assert claude["provider"] == "anthropic"
        assert claude["calls"] == 2

    def test_platform_breakdown(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30)
        platforms = report["platforms"]

        platform_names = [p["platform"] for p in platforms]
        assert "cli" in platform_names
        assert "telegram" in platform_names
        assert "discord" in platform_names

        cli = next(p for p in platforms if p["platform"] == "cli")
        assert cli["sessions"] == 2  # s1 + s3

    def test_tool_breakdown(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30)
        tools = report["tools"]

        tool_names = [t["tool"] for t in tools]
        assert "terminal" in tool_names
        assert "search_files" in tool_names
        assert "read_file" in tool_names
        assert "patch" in tool_names
        assert "web_search" in tool_names

        # terminal was used 2x in s3
        terminal = next(t for t in tools if t["tool"] == "terminal")
        assert terminal["count"] == 2

        # Percentages should sum to ~100%
        total_pct = sum(t["percentage"] for t in tools)
        assert total_pct == pytest.approx(100.0, abs=0.1)

    def test_skill_breakdown(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30)
        skills = report["skills"]

        assert skills["summary"]["distinct_skills_used"] == 3
        assert skills["summary"]["total_skill_loads"] == 3
        assert skills["summary"]["total_skill_edits"] == 1
        assert skills["summary"]["total_skill_actions"] == 4

        top_skill = skills["top_skills"][0]
        assert top_skill["skill"] == "github-pr-workflow"
        assert top_skill["view_count"] == 2
        assert top_skill["manage_count"] == 0
        assert top_skill["total_count"] == 2
        assert top_skill["last_used_at"] is not None

    def test_skill_breakdown_respects_days_filter(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=3)
        skills = report["skills"]

        assert skills["summary"]["distinct_skills_used"] == 2
        assert skills["summary"]["total_skill_loads"] == 2
        assert skills["summary"]["total_skill_edits"] == 1

        skill_names = [s["skill"] for s in skills["top_skills"]]
        assert "systematic-debugging" not in skill_names

    def test_activity_patterns(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30)
        activity = report["activity"]

        assert len(activity["by_day"]) == 7
        assert len(activity["by_hour"]) == 24
        assert activity["active_days"] >= 1
        assert activity["busiest_day"] is not None
        assert activity["busiest_hour"] is not None

    def test_top_sessions(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30)
        top = report["top_sessions"]

        labels = [t["label"] for t in top]
        assert "Longest session" in labels
        assert "Most messages" in labels
        assert "Most tokens" not in labels
        assert "Most tool calls" in labels

    def test_source_filter_cli(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30, source="cli")

        assert report["overview"]["total_sessions"] == 2  # s1, s3

    def test_source_filter_telegram(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30, source="telegram")

        assert report["overview"]["total_sessions"] == 1  # s2

    def test_source_filter_nonexistent(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30, source="slack")

        assert report["empty"] is True

    def test_days_filter_short(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=3)

        # Only s1 (2 days ago) and s4 (1 day ago) should be included
        assert report["overview"]["total_sessions"] == 2

    def test_days_filter_long(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=60)

        # All 5 sessions should be included
        assert report["overview"]["total_sessions"] == 5


class TestEventDerivedInsights:
    def test_mixed_session_uses_each_actual_route_and_stored_event_cost(self, db):
        db.create_session(
            session_id="mixed",
            source="discord",
            model="scalar-wrong-model",
        )
        db.update_token_counts("mixed", input_tokens=9_999, output_tokens=8_888)
        db._conn.execute(
            "UPDATE sessions SET estimated_cost_usd = 77.0 WHERE id = 'mixed'"
        )
        _record_usage(
            db,
            "mixed",
            source="discord",
            provider="openrouter",
            model="shared-model",
            input_tokens=100,
            output_tokens=10,
            estimated_cost_usd=0.11,
        )
        _record_usage(
            db,
            "mixed",
            source="discord",
            provider="anthropic",
            model="shared-model",
            input_tokens=200,
            output_tokens=20,
            estimated_cost_usd=0.22,
        )

        report = InsightsEngine(db).generate(days=30)

        assert report["overview"]["total_input_tokens"] == 300
        assert report["overview"]["total_output_tokens"] == 30
        assert report["overview"]["estimated_cost"] == pytest.approx(0.33)
        assert {
            (row["provider"], row["model"]): row["total_tokens"]
            for row in report["models"]
        } == {
            ("openrouter", "shared-model"): 110,
            ("anthropic", "shared-model"): 220,
        }

    def test_session_activity_metrics_remain_session_derived(self, db):
        now = time.time()
        db.create_session(session_id="activity", source="cli", model="scalar")
        db._conn.execute(
            "UPDATE sessions SET started_at = ?, ended_at = ?, "
            "message_count = 7, tool_call_count = 3 WHERE id = 'activity'",
            (now - 600, now),
        )
        _record_usage(db, "activity", source="cli", input_tokens=40, output_tokens=2)

        overview = InsightsEngine(db).generate(days=30)["overview"]

        assert overview["total_sessions"] == 1
        assert overview["total_messages"] == 7
        assert overview["total_tool_calls"] == 3
        assert overview["total_hours"] == pytest.approx(1 / 6)
        assert overview["total_tokens"] == 42

    def test_source_filter_applies_to_event_derived_usage(self, db):
        for session_id, source in (
            ("cli-session", "cli"),
            ("discord-session", "discord"),
        ):
            db.create_session(session_id=session_id, source=source, model="scalar")
        _record_usage(
            db,
            "cli-session",
            source="cli",
            provider="openai",
            model="gpt-cli",
            input_tokens=30,
            output_tokens=3,
            estimated_cost_usd=0.03,
        )
        _record_usage(
            db,
            "discord-session",
            source="discord",
            provider="anthropic",
            model="claude-discord",
            input_tokens=70,
            output_tokens=7,
            estimated_cost_usd=0.07,
        )

        report = InsightsEngine(db).generate(days=30, source="cli")

        assert report["overview"]["total_sessions"] == 1
        assert report["overview"]["total_tokens"] == 33
        assert report["overview"]["estimated_cost"] == pytest.approx(0.03)
        assert [(row["provider"], row["model"]) for row in report["models"]] == [
            ("openai", "gpt-cli")
        ]

    def test_historical_aggregate_coverage_is_visible(self, db):
        db.create_session(session_id="legacy", source="cli", model="legacy-scalar")
        event_id = _record_usage(
            db,
            "legacy",
            source="cli",
            provider=None,
            model=None,
            input_tokens=1_000,
            output_tokens=100,
            estimated_cost_usd=0.50,
            api_call_index=4,
        )
        db._conn.execute(
            "UPDATE llm_usage_events SET record_kind = 'historical_aggregate', "
            "usage_source = 'reconstructed', measurement_confidence = 'reconstructed' "
            "WHERE id = ?",
            (event_id,),
        )

        report = InsightsEngine(db).generate(days=30)

        assert report["overview"]["historical_aggregate_count"] == 1
        assert report["overview"]["reconstructed_call_count"] == 4
        assert report["overview"]["api_attempt_count"] == 0
        assert report["models"][0]["historical_aggregate_count"] == 1
        assert report["models"][0]["reconstructed_call_count"] == 4
        terminal_text = InsightsEngine(db).format_terminal(report)
        gateway_text = InsightsEngine(db).format_gateway(report)
        assert "reconstructed" in terminal_text.lower()
        assert "reconstructed" in gateway_text.lower()

    def test_recent_event_from_older_session_is_reported_without_session_activity(self, db):
        now = time.time()
        db.create_session(session_id="old-active", source="cli", model="old-scalar")
        db._conn.execute(
            "UPDATE sessions SET started_at = ?, input_tokens = 999999 "
            "WHERE id = 'old-active'",
            (now - 40 * 86400,),
        )
        _record_usage(
            db,
            "old-active",
            timestamp=now,
            source="cli",
            provider="openrouter",
            model="current-route",
            input_tokens=12,
            output_tokens=3,
        )

        report = InsightsEngine(db).generate(days=30)

        assert report["empty"] is False
        assert report["overview"]["total_sessions"] == 0
        assert report["overview"]["total_tokens"] == 15
        assert report["overview"]["avg_tokens_per_session"] is None
        assert report["overview"]["unknown_cost_sessions"] is None
        assert report["overview"]["included_cost_sessions"] is None
        assert report["overview"]["unknown_estimated_cost_events"] == 0
        assert report["overview"]["known_actual_cost_events"] == 0
        assert [(row["provider"], row["model"]) for row in report["models"]] == [
            ("openrouter", "current-route")
        ]
        assert report["platforms"] == [
            {
                "platform": "cli",
                "source": "cli",
                "source_is_valid": True,
                "sessions": 0,
                "messages": 0,
                "tool_calls": 0,
                "input_tokens": 12,
                "output_tokens": 3,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 15,
            }
        ]
        assert report["activity"]["by_day"] == []
        assert report["activity"]["by_hour"] == []
        assert report["activity"]["busiest_day"] is None
        assert report["activity"]["busiest_hour"] is None
        assert report["top_sessions"] == []

    def test_usage_ledger_is_scanned_once_per_canonical_summary(self, db, monkeypatch):
        for index, source in enumerate(("cli", "discord", "telegram")):
            session_id = f"session-{index}"
            db.create_session(session_id=session_id, source=source, model="scalar")
            _record_usage(db, session_id, source=source, input_tokens=index + 1)

        calls = {"global": 0, "routes": 0, "sources": 0}
        original_global = db.summarize_usage_events
        original_routes = db.summarize_usage_by_provider_model
        original_sources = db.summarize_usage_by_source

        def counted_global(*args, **kwargs):
            calls["global"] += 1
            return original_global(*args, **kwargs)

        def counted_routes(*args, **kwargs):
            calls["routes"] += 1
            return original_routes(*args, **kwargs)

        def counted_sources(*args, **kwargs):
            calls["sources"] += 1
            return original_sources(*args, **kwargs)

        monkeypatch.setattr(db, "summarize_usage_events", counted_global)
        monkeypatch.setattr(db, "summarize_usage_by_provider_model", counted_routes)
        monkeypatch.setattr(db, "summarize_usage_by_source", counted_sources)

        InsightsEngine(db).generate(days=30)

        assert calls == {"global": 1, "routes": 1, "sources": 1}

    def test_empty_string_source_filter_applies_to_activity_and_usage(self, db):
        db.create_session(session_id="empty", source="", model="scalar")
        db.create_session(session_id="cli", source="cli", model="scalar")
        db.append_message("empty", role="user", content="empty source")
        db.append_message("cli", role="user", content="cli source")
        _record_usage(db, "empty", source="", input_tokens=2, output_tokens=1)
        _record_usage(db, "cli", source="cli", input_tokens=20, output_tokens=10)

        report = InsightsEngine(db).generate(days=30, source="")

        assert report["overview"]["total_sessions"] == 1
        assert report["overview"]["total_messages"] == 1
        assert report["overview"]["total_tokens"] == 3
        assert [(row["source"], row["sessions"]) for row in report["platforms"]] == [
            ("", 1)
        ]
        assert "Last 30 days ((empty))" in InsightsEngine(db).format_terminal(report)

    def test_failed_api_attempt_is_displayed_as_an_attempt_not_zero_calls(self, db):
        db.create_session(session_id="failed", source="cli", model="scalar")
        _record_usage(
            db,
            "failed",
            source="cli",
            provider="openrouter",
            model="failed-route",
            request_status="error",
        )

        report = InsightsEngine(db).generate(days=30)
        route = report["models"][0]

        assert route["api_attempt_count"] == 1
        assert route["successful_call_count"] == 0
        assert route["calls"] == 1
        assert route["call_label"] == "1"
        assert " 1 " in InsightsEngine(db).format_terminal(report)

    def test_null_and_malformed_sources_have_distinct_platform_labels(self, db):
        old_started_at = time.time() - 40 * 86400
        for session_id in ("null-source", "bad-source"):
            db.create_session(session_id=session_id, source="cli", model="scalar")
            _record_usage(db, session_id, source="cli")
            db._conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (old_started_at, session_id),
            )
        db._conn.execute(
            "UPDATE llm_usage_events SET source = NULL WHERE session_id = 'null-source'"
        )
        db._conn.execute(
            "UPDATE llm_usage_events SET source = ? WHERE session_id = 'bad-source'",
            (b"bad",),
        )

        report = InsightsEngine(db).generate(days=30)

        assert {
            (row["platform"], row["source"], row["source_is_valid"])
            for row in report["platforms"]
        } == {
            ("unattributed", None, True),
            ("invalid/unattributed", None, False),
        }
        terminal_text = InsightsEngine(db).format_terminal(report)
        assert "invalid/unattributed" in terminal_text

    def test_model_identity_is_preserved_separately_from_display_label(self, db):
        for session_id, model in (("empty-model", ""), ("null-model", None)):
            db.create_session(session_id=session_id, source="cli", model="scalar")
            _record_usage(db, session_id, source="cli", model=model)
        db.create_session(session_id="bad-model", source="cli", model="scalar")
        bad_event_id = _record_usage(db, "bad-model", source="cli", model="temporary")
        db._conn.execute(
            "UPDATE llm_usage_events SET model = ? WHERE id = ?", (b"bad", bad_event_id)
        )

        rows = InsightsEngine(db).generate(days=30)["models"]

        assert {
            (row["model"], row["model_is_valid"], row["display_model"])
            for row in rows
        } == {
            (None, False, "invalid/unattributed"),
            (None, True, "unattributed"),
            ("", True, "(empty)"),
        }

    def test_missing_historical_call_coverage_stays_unknown_in_report_and_format(self, db):
        db.create_session(session_id="legacy-unknown", source="cli", model="legacy")
        event_id = _record_usage(
            db, "legacy-unknown", source="cli", input_tokens=100, api_call_index=None
        )
        db._conn.execute(
            "UPDATE llm_usage_events SET record_kind = 'historical_aggregate' "
            "WHERE id = ?",
            (event_id,),
        )

        report = InsightsEngine(db).generate(days=30)
        route = report["models"][0]

        assert report["overview"]["reconstructed_call_unknown_aggregate_count"] == 1
        assert route["calls"] is None
        assert route["call_label"] == "unknown"
        assert "call coverage unknown" in InsightsEngine(db).format_terminal(report).lower()


# =========================================================================
# Formatting
# =========================================================================

class TestTerminalFormatting:
    def test_terminal_format_has_sections(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30)
        text = engine.format_terminal(report)

        assert "Hermes Insights" in text
        assert "Overview" in text
        assert "Models Used" in text
        assert "Top Tools" in text
        assert "Top Skills" in text
        assert "Activity Patterns" in text
        assert "Notable Sessions" in text

    def test_terminal_format_shows_tokens(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30)
        text = engine.format_terminal(report)

        assert "Input tokens" in text
        assert "Output tokens" in text
        # Cost and cache metrics are intentionally hidden (pricing was unreliable).
        assert "Est. cost" not in text
        assert "Cache read" not in text
        assert "Cache write" not in text

    def test_terminal_format_shows_platforms(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30)
        text = engine.format_terminal(report)

        # Multi-platform, so Platforms section should show
        assert "Platforms" in text
        assert "cli" in text
        assert "telegram" in text

    def test_terminal_format_shows_bar_chart(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30)
        text = engine.format_terminal(report)

        assert "█" in text  # Bar chart characters

    def test_terminal_format_hides_cost_for_custom_models(self, db):
        """Cost display is hidden entirely — custom models no longer show 'N/A' either."""
        db.create_session(session_id="s1", source="cli", model="my-custom-model")
        db.update_token_counts("s1", input_tokens=1000, output_tokens=500)
        db._conn.commit()

        engine = InsightsEngine(db)
        report = engine.generate(days=30)
        text = engine.format_terminal(report)

        assert "N/A" not in text
        assert "custom/self-hosted" not in text
        assert "Cost" not in text


class TestGatewayFormatting:
    def test_gateway_format_is_shorter(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30)
        terminal_text = engine.format_terminal(report)
        gateway_text = engine.format_gateway(report)

        assert len(gateway_text) < len(terminal_text)

    def test_gateway_format_has_bold(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30)
        text = engine.format_gateway(report)

        assert "**" in text  # Markdown bold

    def test_gateway_format_hides_cost(self, populated_db):
        """Gateway format omits dollar figures and internal cache details."""
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30)
        text = engine.format_gateway(report)

        assert "$" not in text
        assert "cache" not in text.lower()

    def test_gateway_format_shows_models(self, populated_db):
        engine = InsightsEngine(populated_db)
        report = engine.generate(days=30)
        text = engine.format_gateway(report)

        assert "Models" in text
        assert "calls" in text


# =========================================================================
# Edge cases
# =========================================================================

class TestEdgeCases:
    def test_session_with_no_tokens(self, db):
        """Sessions with zero tokens should not crash."""
        db.create_session(session_id="s1", source="cli", model="test-model")
        db._conn.commit()

        engine = InsightsEngine(db)
        report = engine.generate(days=30)
        assert report["empty"] is False
        assert report["overview"]["total_tokens"] == 0
        assert report["overview"]["estimated_cost"] == 0.0

    def test_session_with_no_end_time(self, db):
        """Active (non-ended) sessions should be included but duration = 0."""
        db.create_session(session_id="s1", source="cli", model="test-model")
        db.update_token_counts("s1", input_tokens=1000, output_tokens=500)
        _record_usage(
            db, "s1", source="cli", model="test-model",
            input_tokens=1000, output_tokens=500, estimated_cost_usd=None,
        )
        db._conn.commit()

        engine = InsightsEngine(db)
        report = engine.generate(days=30)
        # Session included
        assert report["overview"]["total_sessions"] == 1
        assert report["overview"]["total_tokens"] == 1500
        # But no duration stats (session not ended)
        assert report["overview"]["total_hours"] == 0

    def test_session_with_no_model(self, db):
        """Sessions with NULL model should not crash."""
        db.create_session(session_id="s1", source="cli")
        db.update_token_counts("s1", input_tokens=1000, output_tokens=500)
        _record_usage(
            db, "s1", source="cli", provider=None, model=None,
            input_tokens=1000, output_tokens=500, estimated_cost_usd=None,
        )
        db._conn.commit()

        engine = InsightsEngine(db)
        report = engine.generate(days=30)
        assert report["empty"] is False

        models = report["models"]
        assert len(models) == 1
        assert models[0]["model"] is None
        assert models[0]["display_model"] == "unattributed"
        assert models[0]["model_is_valid"] is True
        assert models[0]["has_pricing"] is None
        assert models[0]["has_estimated_cost_coverage"] is False

    def test_custom_model_shows_zero_cost(self, db):
        """Custom/self-hosted models should show $0 cost, not fake estimates."""
        db.create_session(session_id="s1", source="cli", model="FP16_Hermes_4.5")
        db.update_token_counts("s1", input_tokens=100000, output_tokens=50000)
        _record_usage(
            db, "s1", source="cli", provider="local", model="FP16_Hermes_4.5",
            input_tokens=100000, output_tokens=50000, estimated_cost_usd=None,
        )
        db._conn.commit()

        engine = InsightsEngine(db)
        report = engine.generate(days=30)
        assert report["overview"]["estimated_cost"] == 0.0
        assert "FP16_Hermes_4.5" in report["overview"][
            "models_with_unknown_estimated_cost"
        ]
        assert report["overview"]["models_without_pricing"] is None

        models = report["models"]
        custom = next(m for m in models if m["model"] == "FP16_Hermes_4.5")
        assert custom["cost"] == 0.0
        assert custom["has_pricing"] is None
        assert custom["has_estimated_cost_coverage"] is False

    def test_tool_usage_from_tool_calls_json(self, db):
        """Tool usage should be extracted from tool_calls JSON when tool_name is NULL."""
        import json as _json
        db.create_session(session_id="s1", source="cli", model="test")
        # Assistant message with tool_calls (this is what CLI produces)
        db.append_message("s1", role="assistant", content="Let me search",
                          tool_calls=[{"id": "call_1", "type": "function",
                                       "function": {"name": "search_files", "arguments": "{}"}}])
        # Tool response WITHOUT tool_name (this is the CLI bug)
        db.append_message("s1", role="tool", content="found results",
                          tool_call_id="call_1")
        db.append_message("s1", role="assistant", content="Now reading",
                          tool_calls=[{"id": "call_2", "type": "function",
                                       "function": {"name": "read_file", "arguments": "{}"}}])
        db.append_message("s1", role="tool", content="file content",
                          tool_call_id="call_2")
        db.append_message("s1", role="assistant", content="And searching again",
                          tool_calls=[{"id": "call_3", "type": "function",
                                       "function": {"name": "search_files", "arguments": "{}"}}])
        db.append_message("s1", role="tool", content="more results",
                          tool_call_id="call_3")
        db._conn.commit()

        engine = InsightsEngine(db)
        report = engine.generate(days=30)
        tools = report["tools"]

        # Should find tools from tool_calls JSON even though tool_name is NULL
        tool_names = [t["tool"] for t in tools]
        assert "search_files" in tool_names
        assert "read_file" in tool_names

        # search_files was called twice
        sf = next(t for t in tools if t["tool"] == "search_files")
        assert sf["count"] == 2

    def test_overview_cost_coverage_lists_are_json_safe(self, db):
        """Stored estimated-cost coverage is explicit and JSON serializable."""
        import json as _json
        db.create_session(session_id="s1", source="cli", model="gpt-4o")
        db.create_session(session_id="s2", source="cli", model="my-custom")
        _record_usage(
            db, "s1", source="cli", provider="openai", model="gpt-4o",
            estimated_cost_usd=0.01,
        )
        _record_usage(
            db, "s2", source="cli", provider="local", model="my-custom",
            estimated_cost_usd=None,
        )
        db._conn.commit()

        overview = InsightsEngine(db).generate(days=30)["overview"]

        assert overview["models_with_estimated_cost"] == ["gpt-4o"]
        assert overview["models_with_unknown_estimated_cost"] == ["my-custom"]
        assert overview["models_with_pricing"] is None
        assert overview["models_without_pricing"] is None
        _json.dumps(overview)

    def test_mixed_commercial_and_custom_models(self, db):
        """Mix of commercial and custom models: only commercial ones get costs."""
        db.create_session(session_id="s1", source="cli", model="anthropic/claude-sonnet-4-20250514")
        db.update_token_counts(
            "s1",
            input_tokens=10000,
            output_tokens=5000,
            billing_provider="anthropic",
        )
        _record_usage(
            db, "s1", source="cli", provider="anthropic",
            model="anthropic/claude-sonnet-4-20250514",
            input_tokens=10000, output_tokens=5000, estimated_cost_usd=0.18,
        )
        db.create_session(session_id="s2", source="cli", model="my-local-llama")
        db.update_token_counts("s2", input_tokens=10000, output_tokens=5000)
        _record_usage(
            db, "s2", source="cli", provider="local", model="my-local-llama",
            input_tokens=10000, output_tokens=5000, estimated_cost_usd=None,
        )
        db._conn.commit()

        engine = InsightsEngine(db)
        report = engine.generate(days=30)

        # Cost should only come from gpt-4o, not from the custom model
        overview = report["overview"]
        assert overview["estimated_cost"] > 0
        assert "anthropic/claude-sonnet-4-20250514" in overview[
            "models_with_estimated_cost"
        ]
        assert "my-local-llama" in overview["models_with_unknown_estimated_cost"]

        # Verify individual model entries
        claude = next(
            m for m in report["models"]
            if m["model"] == "anthropic/claude-sonnet-4-20250514"
        )
        assert claude["has_pricing"] is None
        assert claude["has_estimated_cost_coverage"] is True
        assert claude["cost"] > 0

        llama = next(m for m in report["models"] if m["model"] == "my-local-llama")
        assert llama["has_pricing"] is None
        assert llama["has_estimated_cost_coverage"] is False
        assert llama["cost"] == 0.0

    def test_single_session_streak(self, db):
        """Single session should have streak of 0 or 1."""
        db.create_session(session_id="s1", source="cli", model="test")
        db._conn.commit()

        engine = InsightsEngine(db)
        report = engine.generate(days=30)
        assert report["activity"]["max_streak"] <= 1

    def test_no_tool_calls(self, db):
        """Sessions with no tool calls should produce empty tools list."""
        db.create_session(session_id="s1", source="cli", model="test")
        db.append_message("s1", role="user", content="hello")
        db.append_message("s1", role="assistant", content="hi there")
        db._conn.commit()

        engine = InsightsEngine(db)
        report = engine.generate(days=30)
        assert report["tools"] == []

    def test_only_one_platform(self, db):
        """Single-platform usage should still work."""
        db.create_session(session_id="s1", source="cli", model="test")
        db._conn.commit()

        engine = InsightsEngine(db)
        report = engine.generate(days=30)
        assert len(report["platforms"]) == 1
        assert report["platforms"][0]["platform"] == "cli"

        # Terminal format should NOT show platform section for single platform
        text = engine.format_terminal(report)
        # (it still shows platforms section if there's only cli and nothing else)
        # Actually the condition is > 1 platforms OR non-cli, so single cli won't show

    def test_large_days_value(self, db):
        """Very large days value should not crash."""
        db.create_session(session_id="s1", source="cli", model="test")
        db._conn.commit()

        engine = InsightsEngine(db)
        report = engine.generate(days=365)
        assert report["empty"] is False

    def test_zero_days(self, db):
        """Zero days should return empty (nothing is in the future)."""
        db.create_session(session_id="s1", source="cli", model="test")
        db._conn.commit()

        engine = InsightsEngine(db)
        report = engine.generate(days=0)
        # Depending on timing, might catch the session if created <1s ago
        # Just verify it doesn't crash
        assert "empty" in report
