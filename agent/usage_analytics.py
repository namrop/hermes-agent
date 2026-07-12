"""Pure aggregation for canonical LLM usage-event read models.

The public functions consume dictionaries (normally projected
``llm_usage_events`` rows) and return strict-JSON-safe dictionaries/lists. A
summary dictionary has these stable keys:

``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
``cache_write_tokens``, ``reasoning_tokens``, ``prompt_tokens``,
``total_tokens``, ``event_count``, ``api_attempt_count``,
``successful_call_count``, ``latency_sample_count``, ``latency_total_ms``,
``average_latency_ms``, ``historical_aggregate_count``,
``reconstructed_call_count``, ``estimated_cost_usd``, ``actual_cost_usd``,
``estimated_cost_known_event_count``, ``estimated_cost_unknown_event_count``,
``actual_cost_known_event_count``, ``actual_cost_unknown_event_count``,
``invalid_numeric_event_count``, and ``invalid_numeric_value_count``.

Numeric normalization policy
----------------------------
Token buckets and historical reconstructed-call counts accept only finite,
integral ``int``/``float`` values (not booleans). Missing values mean zero.
Negative values are invalid unless the row is ``record_kind='correction'``;
reconstructed-call counts belong only to historical rows and are always
nonnegative. Costs accept finite numbers, with negative costs allowed only on
correction rows. A missing cost is unknown but not invalid; an invalid cost is
also unknown and contributes zero. Latency is inspected only for API attempts:
a missing sample is ignored, while a finite nonnegative number (including
fractional milliseconds) is accepted. Every inspected invalid value increments
``invalid_numeric_value_count`` and each affected event increments
``invalid_numeric_event_count`` once.

Grouped rows add their dimensions: provider/model rows add ``provider`` and
``model``; session-route rows additionally add ``purpose``; daily rows add a
``date`` that is either ISO-8601 or the explicit ``unknown`` bucket. Malformed,
non-finite, or platform-out-of-range timestamps use ``unknown``. Timestamp
corruption is grouping metadata, not a numeric accounting value, so it does not
increment the accounting-invalid counters. Costs are accumulated with Decimal
state and exposed as unrounded finite JSON floats; if a finite ledger's sum is
outside the JSON-float range, it is saturated at the largest signed finite
float. Reasoning tokens are an output annotation and are never added to
``total_tokens``. A NULL ``record_kind`` is a legacy API attempt; historical
aggregates contribute token/cost facts but not attempt, success, or latency
metrics.
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import Any, Iterable, Mapping, Optional, Sequence, cast
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
_FLOAT_MAX = sys.float_info.max
_FLOAT_MAX_DECIMAL = Decimal(str(_FLOAT_MAX))
_DECIMAL_PRECISION = 50


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
        "invalid_numeric_event_count": 0,
        "invalid_numeric_value_count": 0,
    }


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _integral_value(value: Any, *, allow_negative: bool) -> Optional[int]:
    if not _finite_number(value):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    result = int(value)
    if result < 0 and not allow_negative:
        return None
    return result


def _finite_decimal(value: Any, *, allow_negative: bool) -> Optional[Decimal]:
    if not _finite_number(value):
        return None
    if value < 0 and not allow_negative:
        return None
    return Decimal(str(value))


def _finite_float(value: Decimal) -> float:
    """Convert Decimal state to a finite JSON float without display rounding."""
    if value > _FLOAT_MAX_DECIMAL:
        return _FLOAT_MAX
    if value < -_FLOAT_MAX_DECIMAL:
        return -_FLOAT_MAX
    return float(value)


def _latency_json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return _finite_float(value)


class _SummaryAccumulator:
    """Bounded, single-pass state for one canonical summary row."""

    __slots__ = ("summary", "estimated_cost", "actual_cost", "latency_total")

    def __init__(self) -> None:
        self.summary = _empty_summary()
        self.estimated_cost = Decimal(0)
        self.actual_cost = Decimal(0)
        self.latency_total = Decimal(0)

    def add(self, event: UsageEvent) -> None:
        summary = self.summary
        summary["event_count"] += 1
        invalid_values = 0

        record_kind = event.get("record_kind")
        if record_kind is None:
            record_kind = "api_attempt"
        is_attempt = record_kind == "api_attempt"
        is_historical = record_kind == "historical_aggregate"
        is_correction = record_kind == "correction"

        for key in _TOKEN_KEYS:
            raw_value = event.get(key)
            if raw_value is None:
                continue
            value = _integral_value(raw_value, allow_negative=is_correction)
            if value is None:
                invalid_values += 1
            else:
                summary[key] += value

        if is_attempt:
            summary["api_attempt_count"] += 1
            if event.get("request_status") == "ok":
                summary["successful_call_count"] += 1
            raw_latency = event.get("latency_ms")
            if raw_latency is not None:
                latency = _finite_decimal(raw_latency, allow_negative=False)
                if latency is None:
                    invalid_values += 1
                else:
                    summary["latency_sample_count"] += 1
                    with localcontext() as context:
                        context.prec = _DECIMAL_PRECISION
                        self.latency_total += latency

        if is_historical:
            summary["historical_aggregate_count"] += 1
            raw_call_count = event.get("api_call_index")
            if raw_call_count is not None:
                call_count = _integral_value(raw_call_count, allow_negative=False)
                if call_count is None:
                    invalid_values += 1
                else:
                    summary["reconstructed_call_count"] += call_count

        for dimension in ("estimated", "actual"):
            cost_key = f"{dimension}_cost_usd"
            known_key = f"{dimension}_cost_known_event_count"
            unknown_key = f"{dimension}_cost_unknown_event_count"
            raw_cost = event.get(cost_key)
            if raw_cost is None:
                summary[unknown_key] += 1
                continue
            cost = _finite_decimal(raw_cost, allow_negative=is_correction)
            if cost is None:
                summary[unknown_key] += 1
                invalid_values += 1
                continue
            summary[known_key] += 1
            with localcontext() as context:
                context.prec = _DECIMAL_PRECISION
                if dimension == "estimated":
                    self.estimated_cost += cost
                else:
                    self.actual_cost += cost

        if invalid_values:
            summary["invalid_numeric_event_count"] += 1
            summary["invalid_numeric_value_count"] += invalid_values

    def finish(self) -> Summary:
        summary = self.summary
        summary["prompt_tokens"] = (
            summary["input_tokens"]
            + summary["cache_read_tokens"]
            + summary["cache_write_tokens"]
        )
        summary["total_tokens"] = summary["prompt_tokens"] + summary["output_tokens"]
        summary["estimated_cost_usd"] = _finite_float(self.estimated_cost)
        summary["actual_cost_usd"] = _finite_float(self.actual_cost)
        summary["latency_total_ms"] = _latency_json_number(self.latency_total)
        if summary["latency_sample_count"]:
            with localcontext() as context:
                context.prec = _DECIMAL_PRECISION
                average = self.latency_total / summary["latency_sample_count"]
            summary["average_latency_ms"] = _finite_float(average)
        return summary


def summarize_usage_events(events: Iterable[UsageEvent]) -> Summary:
    """Aggregate an event iterable once using canonical numeric semantics."""
    accumulator = _SummaryAccumulator()
    for event in events:
        accumulator.add(event)
    return accumulator.finish()


def _sort_value(value: Any) -> tuple[bool, str]:
    """Order known dimensions lexically and keep NULL groups deterministic."""
    return value is None, "" if value is None else str(value)


def _group_usage_events(
    events: Iterable[UsageEvent], dimensions: Sequence[str]
) -> list[Summary]:
    grouped: dict[tuple[Any, ...], _SummaryAccumulator] = {}
    for event in events:
        identity = tuple(event.get(dimension) for dimension in dimensions)
        accumulator = grouped.get(identity)
        if accumulator is None:
            accumulator = grouped[identity] = _SummaryAccumulator()
        accumulator.add(event)

    rows: list[Summary] = []
    for identity, accumulator in grouped.items():
        row = accumulator.finish()
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


def _event_date(event: UsageEvent, target_timezone: Any) -> str:
    timestamp = event.get("timestamp")
    if not _finite_number(timestamp):
        return "unknown"
    try:
        instant = datetime.fromtimestamp(cast("int | float", timestamp), timezone.utc)
        local_instant = (
            instant.astimezone(target_timezone)
            if target_timezone is not None
            else instant.astimezone()
        )
    except (OverflowError, OSError, ValueError):
        return "unknown"
    return local_instant.date().isoformat()


def summarize_usage_daily(
    events: Iterable[UsageEvent], *, timezone_name: Optional[str] = None
) -> list[Summary]:
    """Group once by ISO event date, or ``unknown`` for invalid timestamps.

    ``timezone_name=None`` uses the process's local timezone. Explicit names
    use :class:`zoneinfo.ZoneInfo`; invalid IANA names raise ``ValueError``.
    SQL cutoffs necessarily exclude malformed timestamps because they cannot
    satisfy a numeric comparison; without a cutoff those rows remain visible in
    the deterministic ``unknown`` bucket.
    """
    target_timezone = _daily_timezone(timezone_name)
    grouped: dict[str, _SummaryAccumulator] = {}
    for event in events:
        date = _event_date(event, target_timezone)
        accumulator = grouped.get(date)
        if accumulator is None:
            accumulator = grouped[date] = _SummaryAccumulator()
        accumulator.add(event)

    rows: list[Summary] = []
    for date in sorted(grouped, key=lambda value: (value == "unknown", value)):
        row = grouped[date].finish()
        row["date"] = date
        rows.append(row)
    return rows
