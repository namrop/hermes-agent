"""Persisted session-usage reporting shared by CLI and gateway surfaces.

The event ledger is authoritative for cumulative usage. Resident-agent state is
intentionally excluded here; callers may append live context pressure, rate
limits, and account snapshots separately.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

UsageReport = dict[str, Any]
Translator = Callable[..., str]


def _safe_nonnegative_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        result = int(value)
        return result if result >= 0 else None
    return None


def _valid_report(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    summary = value.get("summary")
    routes = value.get("routes")
    if not isinstance(summary, Mapping) or not isinstance(routes, Sequence):
        return False
    if isinstance(routes, (str, bytes, bytearray)):
        return False
    return all(isinstance(route, Mapping) for route in routes)


def read_persisted_session_usage(
    db: Any, session_id: Optional[str]
) -> Optional[UsageReport]:
    """Return one validated, reconciled session report, or ``None``."""
    if db is None or not session_id:
        return None
    try:
        value = db.summarize_session_usage_report(session_id)
    except Exception:
        return None
    if not _valid_report(value):
        return None
    summary = value["summary"]
    event_count = _safe_nonnegative_int(summary.get("event_count"))
    if not event_count:
        return None
    return {
        "summary": dict(summary),
        "routes": [dict(route) for route in value["routes"]],
    }


def _display_dimension(value: Any, is_valid: bool, *, render: Translator) -> str:
    if not is_valid:
        return render("dimension_invalid")
    if value is None:
        return render("dimension_unattributed")
    if value == "":
        return render("dimension_empty")
    return str(value)


def _call_count(summary: Mapping[str, Any]) -> Optional[int]:
    unknown = _safe_nonnegative_int(
        summary.get("reconstructed_call_unknown_aggregate_count")
    )
    if unknown is None or unknown:
        return None
    attempts = _safe_nonnegative_int(summary.get("api_attempt_count"))
    reconstructed = _safe_nonnegative_int(summary.get("reconstructed_call_count"))
    if attempts is None or reconstructed is None:
        return None
    return attempts + reconstructed


def _exact_decimal_text(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    if not parsed.is_finite():
        return None
    return value


def _cost_text(
    exact: Any,
    unknown_event_count: Any,
    *,
    subscription_included_event_count: Any = 0,
    known_event_count: Any = None,
    event_count: Any = None,
    render: Translator,
) -> str:
    exact_text = _exact_decimal_text(exact)
    unknown = _safe_nonnegative_int(unknown_event_count)
    included = _safe_nonnegative_int(subscription_included_event_count)
    known = _safe_nonnegative_int(known_event_count)
    total_events = _safe_nonnegative_int(event_count)
    if exact_text is None or unknown is None or included is None:
        return render("cost_unknown")
    if known is None and total_events is not None:
        known = max(0, total_events - unknown)
    if known == 0 and unknown > 0 and included == 0:
        return render("cost_unknown")
    if (
        exact_text == "0"
        and unknown == 0
        and total_events is not None
        and total_events > 0
        and included == total_events
    ):
        return render("cost_included")

    amount = "0" if exact_text == "0" else exact_text
    if unknown and included:
        return render(
            "cost_amount_unknown_included",
            amount=amount,
            unknown=unknown,
            included=included,
        )
    if unknown:
        return render("cost_amount_unknown", amount=amount, count=unknown)
    if included:
        return render("cost_amount_included", amount=amount, count=included)
    return render("cost_amount", amount=amount)


def _number_text(value: Any, *, render: Translator) -> str:
    number = _safe_nonnegative_int(value)
    return render("value_unknown") if number is None else f"{number:,}"


def _count_text(value: Optional[int], *, observed: Any, render: Translator) -> str:
    if value is not None:
        return f"{value:,}"
    observed_number = _safe_nonnegative_int(observed)
    if observed_number is None:
        return render("value_unknown")
    return render("recorded_attempts_unknown", count=f"{observed_number:,}")


def _default_translate(key: str, **values: Any) -> str:
    templates = {
        "header": "📊 Session Token Usage",
        "input": "Input tokens: {count}",
        "cache_read": "Cache read tokens: {count}",
        "cache_write": "Cache write tokens: {count}",
        "output": "Output tokens: {count}",
        "reasoning": "↳ Reasoning (output subset): {count}",
        "prompt": "Prompt tokens (total): {count}",
        "total": "Total tokens: {count}",
        "recorded_calls": "Recorded calls: {count}",
        "estimated_cost": "Estimated cost: {cost}",
        "actual_cost": "Actual cost: {cost}",
        "cost_unknown": "unknown",
        "cost_included": "included",
        "cost_amount": "${amount}",
        "cost_amount_unknown": "${amount} + {count} events unknown",
        "cost_amount_included": "${amount} + {count} subscription-included events",
        "cost_amount_unknown_included": (
            "${amount} + {unknown} events unknown + "
            "{included} subscription-included events"
        ),
        "routes": "Provider/model routes:",
        "route": "- {provider} / {model}",
        "dimension_invalid": "invalid/unattributed",
        "dimension_unattributed": "unattributed",
        "dimension_empty": "(empty)",
        "value_unknown": "unknown",
        "recorded_attempts_unknown": "unknown ({count} recorded attempts)",
    }
    return templates[key].format(**values)


def format_session_usage_lines(
    report: Mapping[str, Any],
    *,
    markdown: bool = False,
    translate: Optional[Translator] = None,
) -> list[str]:
    """Format a validated persisted report without repricing it.

    ``translate`` receives semantic keys from ``_default_translate`` and the
    interpolation values. The gateway supplies its locale adapter; the CLI uses
    the English terminal defaults.
    """
    if not _valid_report(report):
        return [_default_translate("header"), "Usage data unavailable"]
    summary = report["summary"]
    routes = report.get("routes") or []
    render = translate or _default_translate
    header = render("header")
    if markdown and translate is None:
        header = "📊 **Session Token Usage**"
    calls = _call_count(summary)
    observed = summary.get("api_attempt_count")
    included = summary.get("subscription_included_event_count", 0)
    event_count = summary.get("event_count")

    lines = [
        header,
        render("input", count=_number_text(summary.get("input_tokens"), render=render)),
        render("cache_read", count=_number_text(summary.get("cache_read_tokens"), render=render)),
        render("cache_write", count=_number_text(summary.get("cache_write_tokens"), render=render)),
        render("output", count=_number_text(summary.get("output_tokens"), render=render)),
    ]
    reasoning = _safe_nonnegative_int(summary.get("reasoning_tokens"))
    if reasoning:
        lines.append(render("reasoning", count=f"{reasoning:,}"))
    lines.extend(
        [
            render("prompt", count=_number_text(summary.get("prompt_tokens"), render=render)),
            render("total", count=_number_text(summary.get("total_tokens"), render=render)),
            render(
                "recorded_calls",
                count=_count_text(calls, observed=observed, render=render),
            ),
            render(
                "estimated_cost",
                cost=_cost_text(
                    summary.get("estimated_cost_usd_exact"),
                    summary.get("estimated_cost_unknown_event_count"),
                    subscription_included_event_count=included,
                    known_event_count=summary.get(
                        "estimated_cost_known_event_count"
                    ),
                    event_count=event_count,
                    render=render,
                ),
            ),
            render(
                "actual_cost",
                cost=_cost_text(
                    summary.get("actual_cost_usd_exact"),
                    summary.get("actual_cost_unknown_event_count"),
                    known_event_count=summary.get("actual_cost_known_event_count"),
                    event_count=event_count,
                    render=render,
                ),
            ),
        ]
    )

    if routes:
        lines.append("")
        lines.append(render("routes"))
        for route in routes:
            provider = _display_dimension(
                route.get("provider"),
                bool(route.get("provider_is_valid", True)),
                render=render,
            )
            model = _display_dimension(
                route.get("model"),
                bool(route.get("model_is_valid", True)),
                render=render,
            )
            route_calls = _call_count(route)
            route_observed = route.get("api_attempt_count")
            route_included = route.get("subscription_included_event_count", 0)
            route_events = route.get("event_count")
            lines.extend(
                [
                    render("route", provider=provider, model=model),
                    "  " + render("input", count=_number_text(route.get("input_tokens"), render=render)),
                    "  "
                    + render(
                        "cache_read",
                        count=_number_text(route.get("cache_read_tokens"), render=render),
                    ),
                    "  "
                    + render(
                        "cache_write",
                        count=_number_text(route.get("cache_write_tokens"), render=render),
                    ),
                    "  " + render("output", count=_number_text(route.get("output_tokens"), render=render)),
                    "  " + render("total", count=_number_text(route.get("total_tokens"), render=render)),
                    "  "
                    + render(
                        "recorded_calls",
                        count=_count_text(route_calls, observed=route_observed, render=render),
                    ),
                    "  "
                    + render(
                        "estimated_cost",
                        cost=_cost_text(
                            route.get("estimated_cost_usd_exact"),
                            route.get("estimated_cost_unknown_event_count"),
                            subscription_included_event_count=route_included,
                            known_event_count=route.get(
                                "estimated_cost_known_event_count"
                            ),
                            event_count=route_events,
                            render=render,
                        ),
                    ),
                    "  "
                    + render(
                        "actual_cost",
                        cost=_cost_text(
                            route.get("actual_cost_usd_exact"),
                            route.get("actual_cost_unknown_event_count"),
                            event_count=route_events,
                            render=render,
                        ),
                    ),
                ]
            )
    return lines
