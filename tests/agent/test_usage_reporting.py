"""Tests for persisted CLI/gateway session usage reporting."""

from unittest.mock import MagicMock

from agent.usage_reporting import (
    format_session_usage_lines,
    read_persisted_session_usage,
)


def _summary(**overrides):
    values = {
        "event_count": 1,
        "input_tokens": 4,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 2,
        "reasoning_tokens": 0,
        "prompt_tokens": 4,
        "total_tokens": 6,
        "api_attempt_count": 1,
        "reconstructed_call_count": 0,
        "reconstructed_call_unknown_aggregate_count": 0,
        "estimated_cost_usd_exact": "0.01",
        "estimated_cost_unknown_event_count": 0,
        "actual_cost_usd_exact": "0",
        "actual_cost_unknown_event_count": 1,
        "subscription_included_event_count": 0,
    }
    values.update(overrides)
    return values


def test_read_uses_single_atomic_session_report_query():
    db = MagicMock()
    db.summarize_session_usage_report.return_value = {
        "summary": _summary(),
        "routes": [],
    }

    report = read_persisted_session_usage(db, "session-1")

    assert report is not None
    db.summarize_session_usage_report.assert_called_once_with("session-1")
    db.summarize_usage_events.assert_not_called()
    db.summarize_usage_by_provider_model.assert_not_called()


def test_read_rejects_malformed_report_shapes_without_crashing():
    db = MagicMock()
    malformed = [
        ["not", "a", "mapping"],
        {"summary": ["not", "a", "mapping"], "routes": []},
        {"summary": _summary(), "routes": ["not-a-route"]},
    ]

    for value in malformed:
        db.summarize_session_usage_report.return_value = value
        assert read_persisted_session_usage(db, "session-1") is None


def test_missing_exact_cost_is_unknown_not_false_zero():
    lines = format_session_usage_lines(
        {"summary": _summary(estimated_cost_usd_exact=None), "routes": []}
    )

    assert "Estimated cost: unknown" in lines
    assert "Estimated cost: $0" not in lines


def test_subscription_included_cost_stays_distinct_from_monetary_zero():
    lines = format_session_usage_lines(
        {
            "summary": _summary(
                estimated_cost_usd_exact="0",
                estimated_cost_unknown_event_count=0,
                subscription_included_event_count=1,
            ),
            "routes": [],
        }
    )

    assert "Estimated cost: included" in lines


def test_report_labels_recorded_calls_not_unobserved_provider_attempts():
    lines = format_session_usage_lines(
        {"summary": _summary(api_attempt_count=2), "routes": []}
    )

    assert "Recorded calls: 2" in lines
    assert not any(line.startswith("API calls:") for line in lines)


def test_malformed_numeric_value_degrades_to_unknown_in_formatter():
    lines = format_session_usage_lines(
        {
            "summary": _summary(input_tokens=None),
            "routes": [],
        }
    )

    assert "Input tokens: unknown" in lines


def test_formatter_localizes_unknown_counts_and_route_dimensions():
    templates = {
        "header": "header",
        "input": "input={count}",
        "cache_read": "cache-read={count}",
        "cache_write": "cache-write={count}",
        "output": "output={count}",
        "reasoning": "reasoning={count}",
        "prompt": "prompt={count}",
        "total": "total={count}",
        "recorded_calls": "calls={count}",
        "estimated_cost": "estimated={cost}",
        "actual_cost": "actual={cost}",
        "cost_unknown": "COST-UNKNOWN",
        "cost_included": "INCLUDED",
        "cost_amount": "${amount}",
        "cost_amount_unknown": "${amount}+{count}-unknown",
        "cost_amount_included": "${amount}+{count}-included",
        "cost_amount_unknown_included": "${amount}+{unknown}+{included}",
        "routes": "routes",
        "route": "route={provider}/{model}",
        "dimension_invalid": "INVALID-DIMENSION",
        "dimension_unattributed": "UNATTRIBUTED",
        "dimension_empty": "EMPTY-DIMENSION",
        "value_unknown": "VALUE-UNKNOWN",
        "recorded_attempts_unknown": "UNKNOWN-CALLS({count})",
    }

    def translate(key, **values):
        return templates[key].format(**values)

    summary = _summary(
        input_tokens=None,
        reconstructed_call_unknown_aggregate_count=1,
    )
    route = dict(summary, provider=None, model=None, provider_is_valid=False)
    lines = format_session_usage_lines(
        {"summary": summary, "routes": [route]}, translate=translate
    )

    assert "input=VALUE-UNKNOWN" in lines
    assert "calls=UNKNOWN-CALLS(1)" in lines
    assert "route=INVALID-DIMENSION/UNATTRIBUTED" in lines
