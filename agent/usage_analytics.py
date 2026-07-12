"""Pure aggregation for canonical LLM usage-event read models.

The public functions consume dictionaries (normally projected
``llm_usage_events`` rows) and return strict-JSON-safe dictionaries/lists. A
summary dictionary has these stable keys:

``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
``cache_write_tokens``, ``reasoning_tokens``, ``prompt_tokens``,
``total_tokens``, ``event_count``, ``api_attempt_count``,
``successful_call_count``, ``latency_sample_count``, ``latency_total_ms``,
``average_latency_ms``, ``historical_aggregate_count``,
``reconstructed_call_count``, ``reconstructed_call_known_aggregate_count``,
``reconstructed_call_unknown_aggregate_count``, compatibility
``estimated_cost_usd`` and ``actual_cost_usd`` floats, authoritative
``estimated_cost_usd_exact`` and ``actual_cost_usd_exact`` decimal strings,
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

Grouped rows add JSON-safe dimensions: schema-conforming provider/model rows add
``provider`` and ``model``; session-route rows additionally add ``purpose``.
Malformed dimensions coalesce into one explicit invalid/unattributed NULL bucket,
distinguished from legitimate NULL by each ``<dimension>_is_valid`` bit.
Daily rows add a ``date`` that is either ISO-8601 or the explicit ``unknown``
bucket. Malformed,
non-finite, or platform-out-of-range timestamps use ``unknown``. Timestamp
corruption is grouping metadata, not a numeric accounting value, so it does not
increment the accounting-invalid counters. Costs accumulate as exact rational
state derived from their canonical decimal spelling, so order and group
boundaries cannot lose cancellation. The ``*_exact`` decimal strings are the
accounting values; compatibility floats are finite when representable and
``None`` otherwise. Reasoning tokens are an output annotation and are never added to
``total_tokens``. A NULL ``record_kind`` is a legacy API attempt; historical
aggregates contribute token/cost facts but not attempt, success, or latency
metrics.
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from fractions import Fraction
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
_FLOAT_MAX_FRACTION = Fraction.from_float(_FLOAT_MAX)
_SQLITE_INT_MIN = -(1 << 63)
_SQLITE_INT_MAX = (1 << 63) - 1


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
        "reconstructed_call_known_aggregate_count": 0,
        "reconstructed_call_unknown_aggregate_count": 0,
        "estimated_cost_usd": 0.0,
        "actual_cost_usd": 0.0,
        "estimated_cost_usd_exact": "0",
        "actual_cost_usd_exact": "0",
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
        # Mirror the range SQLite can persist in an INTEGER column and reject
        # arbitrary Python integers before any string/Decimal conversion.
        return _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX
    return isinstance(value, float) and math.isfinite(value)


def _integral_value(value: Any, *, allow_negative: bool) -> Optional[int]:
    if not _finite_number(value):
        return None
    if isinstance(value, float):
        if (
            not value.is_integer()
            or value < _SQLITE_INT_MIN
            or value > _SQLITE_INT_MAX
        ):
            return None
    result = int(value)
    if result < 0 and not allow_negative:
        return None
    return result


def _finite_fraction(value: Any, *, allow_negative: bool) -> Optional[Fraction]:
    if not _finite_number(value):
        return None
    if value < 0 and not allow_negative:
        return None
    if isinstance(value, int):
        return Fraction(value)
    # Decimal(str(float)) preserves the canonical decimal fact supplied by
    # SQLite/Python rather than importing its binary expansion noise.
    return Fraction(Decimal(str(value)))


def _fraction_to_decimal(value: Fraction) -> Decimal:
    """Convert a finite decimal fraction exactly, independent of add order."""
    if not value:
        return Decimal(0)
    numerator_bits = abs(value.numerator).bit_length()
    denominator_bits = value.denominator.bit_length()
    decimal_digits = int((numerator_bits + denominator_bits) * math.log10(2)) + 10
    with localcontext() as context:
        context.prec = max(32, decimal_digits)
        return Decimal(value.numerator) / Decimal(value.denominator)


def _fraction_to_decimal_string(value: Fraction) -> str:
    decimal_value = _fraction_to_decimal(value)
    text = format(decimal_value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _fraction_to_json_float(value: Fraction) -> Optional[float]:
    """Return a finite compatibility float, or NULL when not representable."""
    if abs(value) > _FLOAT_MAX_FRACTION:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _latency_json_number(value: Fraction) -> int | float | None:
    if value.denominator == 1:
        return value.numerator
    return _fraction_to_json_float(value)


class _SummaryAccumulator:
    """Bounded, single-pass state for one canonical summary row."""

    __slots__ = ("summary", "estimated_cost", "actual_cost", "latency_total")

    def __init__(self) -> None:
        self.summary = _empty_summary()
        self.estimated_cost = Fraction(0)
        self.actual_cost = Fraction(0)
        self.latency_total = Fraction(0)

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
                latency = _finite_fraction(raw_latency, allow_negative=False)
                if latency is None:
                    invalid_values += 1
                else:
                    summary["latency_sample_count"] += 1
                    self.latency_total += latency

        if is_historical:
            summary["historical_aggregate_count"] += 1
            raw_call_count = event.get("api_call_index")
            if raw_call_count is None:
                summary["reconstructed_call_unknown_aggregate_count"] += 1
            else:
                call_count = _integral_value(raw_call_count, allow_negative=False)
                if call_count is None:
                    summary["reconstructed_call_unknown_aggregate_count"] += 1
                    invalid_values += 1
                else:
                    summary["reconstructed_call_known_aggregate_count"] += 1
                    summary["reconstructed_call_count"] += call_count

        for dimension in ("estimated", "actual"):
            cost_key = f"{dimension}_cost_usd"
            known_key = f"{dimension}_cost_known_event_count"
            unknown_key = f"{dimension}_cost_unknown_event_count"
            raw_cost = event.get(cost_key)
            if raw_cost is None:
                summary[unknown_key] += 1
                continue
            cost = _finite_fraction(raw_cost, allow_negative=is_correction)
            if cost is None:
                summary[unknown_key] += 1
                invalid_values += 1
                continue
            summary[known_key] += 1
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
        summary["estimated_cost_usd_exact"] = _fraction_to_decimal_string(
            self.estimated_cost
        )
        summary["actual_cost_usd_exact"] = _fraction_to_decimal_string(self.actual_cost)
        summary["estimated_cost_usd"] = _fraction_to_json_float(self.estimated_cost)
        summary["actual_cost_usd"] = _fraction_to_json_float(self.actual_cost)
        summary["latency_total_ms"] = _latency_json_number(self.latency_total)
        if summary["latency_sample_count"]:
            average = self.latency_total / summary["latency_sample_count"]
            summary["average_latency_ms"] = _fraction_to_json_float(average)
        return summary


def summarize_usage_events(events: Iterable[UsageEvent]) -> Summary:
    """Aggregate an event iterable once using canonical numeric semantics."""
    accumulator = _SummaryAccumulator()
    for event in events:
        accumulator.add(event)
    return accumulator.finish()


def _json_dimension(value: Any) -> tuple[Optional[str], bool]:
    """Return a conservative JSON-safe group value plus validity bit.

    Schema-conforming strings and NULL retain their value. Every malformed
    value is deliberately coalesced into the invalid/unattributed NULL bucket.
    The validity bit keeps that bucket distinct from legitimate NULL, so no
    malformed value can be mistaken for or merged with a valid route identity.
    """
    if value is None or isinstance(value, str):
        return value, True
    return None, False


def _sort_value(value: Any) -> tuple[bool, str]:
    """Order known dimensions lexically and keep NULL groups deterministic."""
    return value is None, "" if value is None else str(value)


def _group_usage_events(
    events: Iterable[UsageEvent], dimensions: Sequence[str]
) -> list[Summary]:
    grouped: dict[tuple[Any, ...], _SummaryAccumulator] = {}
    for event in events:
        identity = tuple(
            _json_dimension(event.get(dimension)) for dimension in dimensions
        )
        accumulator = grouped.get(identity)
        if accumulator is None:
            accumulator = grouped[identity] = _SummaryAccumulator()
        accumulator.add(event)

    rows: list[Summary] = []
    for identity, accumulator in grouped.items():
        row = accumulator.finish()
        for dimension, (value, is_valid) in zip(dimensions, identity):
            row[dimension] = value
            row[f"{dimension}_is_valid"] = is_valid
        rows.append(row)
    rows.sort(
        key=lambda row: tuple(
            (
                _sort_value(row.get(dimension)),
                not bool(row.get(f"{dimension}_is_valid")),
            )
            for dimension in dimensions
        )
    )
    return rows


def summarize_usage_by_provider_model(
    events: Iterable[UsageEvent],
) -> list[Summary]:
    """Group events by billing provider and model without collapsing either."""
    return _group_usage_events(events, ("provider", "model"))


def summarize_usage_by_source(events: Iterable[UsageEvent]) -> list[Summary]:
    """Group events by source while preserving validity and NULL attribution."""
    return _group_usage_events(events, ("source",))


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
