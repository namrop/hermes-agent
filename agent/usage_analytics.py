"""Pure aggregation for canonical LLM usage-event read models.

The public functions consume dictionaries (normally ``llm_usage_events`` rows)
and return JSON-safe dictionaries/lists. A summary dictionary has these stable
keys:

``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
``cache_write_tokens``, ``reasoning_tokens``, ``prompt_tokens``,
``total_tokens``, ``event_count``, ``api_attempt_count``,
``successful_call_count``, ``latency_sample_count``, ``latency_total_ms``,
``average_latency_ms``, ``historical_aggregate_count``,
``reconstructed_call_count``, ``estimated_cost_usd``, ``actual_cost_usd``,
``estimated_cost_known_event_count``, ``estimated_cost_unknown_event_count``,
``actual_cost_known_event_count``, and ``actual_cost_unknown_event_count``.

Grouped rows add their dimensions: provider/model rows add ``provider`` and
``model``; session-route rows additionally add ``purpose``; daily rows add an
ISO ``date``. Costs are unrounded USD floats. Reasoning tokens are an output
annotation and are never added to ``total_tokens``. A NULL ``record_kind`` is a
legacy API attempt; historical aggregates contribute token/cost facts but not
attempt, success, or latency metrics.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UsageEvent = Mapping[str, Any]
Summary = dict[str, Any]

_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _empty_summary() -> Summary:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "prompt_tokens": 0,
        "total_tokens": 0,
        "event_count": 0,
        "api_attempt_count": 0,
        "successful_call_count": 0,
        "latency_sample_count": 0,
        "latency_total_ms": 0,
        "average_latency_ms": None,
        "historical_aggregate_count": 0,
        "reconstructed_call_count": 0,
        "estimated_cost_usd": 0.0,
        "actual_cost_usd": 0.0,
        "estimated_cost_known_event_count": 0,
        "estimated_cost_unknown_event_count": 0,
        "actual_cost_known_event_count": 0,
        "actual_cost_unknown_event_count": 0,
    }


def summarize_usage_events(events: Iterable[UsageEvent]) -> Summary:
    """Aggregate usage events using canonical token/cost/attempt semantics."""
    summary = _empty_summary()

    for event in events:
        summary["event_count"] += 1
        for key in _TOKEN_KEYS:
            summary[key] += _as_int(event.get(key))

        record_kind = event.get("record_kind")
        if record_kind is None:
            record_kind = "api_attempt"
        is_attempt = record_kind == "api_attempt"
        is_historical = record_kind == "historical_aggregate"

        if is_attempt:
            summary["api_attempt_count"] += 1
            if event.get("request_status") == "ok":
                summary["successful_call_count"] += 1
            latency = event.get("latency_ms")
            if latency is not None:
                summary["latency_sample_count"] += 1
                summary["latency_total_ms"] += _as_int(latency)

        if is_historical:
            summary["historical_aggregate_count"] += 1
            summary["reconstructed_call_count"] += _as_int(
                event.get("api_call_index")
            )

        for dimension in ("estimated", "actual"):
            cost_key = f"{dimension}_cost_usd"
            known_key = f"{dimension}_cost_known_event_count"
            unknown_key = f"{dimension}_cost_unknown_event_count"
            cost = event.get(cost_key)
            if cost is None:
                summary[unknown_key] += 1
            else:
                summary[known_key] += 1
                summary[cost_key] += float(cost)

    summary["prompt_tokens"] = (
        summary["input_tokens"]
        + summary["cache_read_tokens"]
        + summary["cache_write_tokens"]
    )
    summary["total_tokens"] = summary["prompt_tokens"] + summary["output_tokens"]
    if summary["latency_sample_count"]:
        summary["average_latency_ms"] = (
            summary["latency_total_ms"] / summary["latency_sample_count"]
        )
    return summary


def _sort_value(value: Any) -> tuple[bool, str]:
    """Order known dimensions lexically and keep NULL groups deterministic."""
    return value is None, "" if value is None else str(value)


def _group_usage_events(
    events: Iterable[UsageEvent], dimensions: Sequence[str]
) -> list[Summary]:
    grouped: dict[tuple[Any, ...], list[UsageEvent]] = defaultdict(list)
    for event in events:
        grouped[tuple(event.get(dimension) for dimension in dimensions)].append(event)

    rows: list[Summary] = []
    for identity, group_events in grouped.items():
        row = summarize_usage_events(group_events)
        row.update(zip(dimensions, identity))
        rows.append(row)
    rows.sort(
        key=lambda row: tuple(_sort_value(row.get(dimension)) for dimension in dimensions)
    )
    return rows


def summarize_usage_by_provider_model(
    events: Iterable[UsageEvent],
) -> list[Summary]:
    """Group events by billing provider and model without collapsing either."""
    return _group_usage_events(events, ("provider", "model"))


def summarize_session_routes(events: Iterable[UsageEvent]) -> list[Summary]:
    """Group one session's events by provider, model, and purpose route."""
    return _group_usage_events(events, ("provider", "model", "purpose"))


def _daily_timezone(timezone_name: Optional[str]):
    if timezone_name is None:
        return None
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Invalid timezone_name: {timezone_name}") from exc


def summarize_usage_daily(
    events: Iterable[UsageEvent], *, timezone_name: Optional[str] = None
) -> list[Summary]:
    """Group by event occurrence date in the requested or local timezone.

    ``timezone_name=None`` uses the process's local timezone. Explicit names
    use :class:`zoneinfo.ZoneInfo`; invalid IANA names raise ``ValueError``.
    """
    target_timezone = _daily_timezone(timezone_name)
    grouped: dict[str, list[UsageEvent]] = defaultdict(list)
    for event in events:
        timestamp = float(event["timestamp"])
        instant = datetime.fromtimestamp(timestamp, timezone.utc)
        local_instant = (
            instant.astimezone(target_timezone)
            if target_timezone is not None
            else instant.astimezone()
        )
        grouped[local_instant.date().isoformat()].append(event)

    rows: list[Summary] = []
    for date in sorted(grouped):
        row = summarize_usage_events(grouped[date])
        row["date"] = date
        rows.append(row)
    return rows
