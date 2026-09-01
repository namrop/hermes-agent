"""Tests for tools/quota_bench.py — pre-emptive quota benching (docs/NEXT.md item 3).

Covers the decision logic only (``evaluate`` is pure); the pool-writing path is
exercised in integration, not here.
"""

import importlib.util
import time
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "quota_bench", Path(__file__).resolve().parent.parent / "tools" / "quota_bench.py"
)
qb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(qb)


NOW = 1_756_000_000.0


def obs(provider, *, used, limit, quota_name="week", confidence="exact",
        resets_at=None, window_kind="fixed", age_seconds=60):
    """Build a quota_observation_v1-shaped record."""
    observed = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(NOW - age_seconds)) + "Z"
    return {
        "provider": provider,
        "quota_name": quota_name,
        "used_value": str(used),
        "limit_value": str(limit),
        "unit": "percent",
        "measurement_confidence": confidence,
        "observed_at": observed,
        "resets_at": resets_at,
        "window_kind": window_kind,
    }


def run(chain, observations, **kw):
    kw.setdefault("threshold_pct", 90.0)
    kw.setdefault("max_age_seconds", 5400.0)
    kw.setdefault("include_estimated", True)
    kw.setdefault("default_bench_seconds", 21600)
    kw.setdefault("now", NOW)
    return qb.evaluate(chain, observations, **kw)


def benched_names(result):
    return {r["pool_provider"] for r in result["benched"]}


def test_under_threshold_is_not_benched():
    r = run(["zai", "openai-codex"],
            {"z-ai": obs("z-ai", used=88, limit=100),
             "openai": obs("openai", used=1, limit=100)})
    assert benched_names(r) == set()
    assert r["fail_open"] is False


def test_at_threshold_benches_to_resets_at_cliff():
    reset_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(NOW + 3600)) + "Z"
    r = run(["zai", "openai-codex"],
            {"z-ai": obs("z-ai", used=90, limit=100, resets_at=reset_iso),
             "openai": obs("openai", used=1, limit=100)})
    assert benched_names(r) == {"zai"}
    row = r["benched"][0]
    assert row["bench_basis"] == "resets_at"
    assert row["bench_until"] == pytest.approx(NOW + 3600, abs=2)


def test_rolling_window_without_resets_at_uses_default_ttl():
    """opencode-go reports a rolling window and no reset cliff."""
    r = run(["opencode-go", "openai-codex"],
            {"opencode-go": obs("opencode-go", used=29, limit=30,
                                confidence="estimated", window_kind="rolling"),
             "openai": obs("openai", used=1, limit=100)},
            default_bench_seconds=7200)
    row = r["benched"][0]
    assert row["pool_provider"] == "opencode-go"
    assert row["bench_basis"] == "rolling window, no resets_at"
    assert row["bench_until"] == pytest.approx(NOW + 7200, abs=2)


def test_fail_open_when_every_assessable_provider_is_over():
    r = run(["zai", "openai-codex"],
            {"z-ai": obs("z-ai", used=99, limit=100),
             "openai": obs("openai", used=95, limit=100)})
    assert r["fail_open"] is True
    assert benched_names(r) == set()


def test_fail_open_ignores_providers_with_no_signal():
    """A no-signal provider must not count as a survivor.

    deepseek reports no weekly window *and* is 402-dead. If it counted as spare
    capacity, this would bench every provider it can see and leave the router
    with nothing — the outage the rule exists to prevent.
    """
    r = run(["zai", "deepseek", "openai-codex"],
            {"z-ai": obs("z-ai", used=99, limit=100),
             "openai": obs("openai", used=95, limit=100)})
    assert r["fail_open"] is True
    assert benched_names(r) == set()


def test_partial_exhaustion_still_benches():
    """The fail-open guard must not over-trigger while a survivor exists."""
    r = run(["zai", "kimi-coding", "openai-codex"],
            {"z-ai": obs("z-ai", used=99, limit=100),
             "kimi-coding": obs("kimi-coding", used=95, limit=100),
             "openai": obs("openai", used=1, limit=100)})
    assert r["fail_open"] is False
    assert benched_names(r) == {"zai", "kimi-coding"}


def test_stale_observation_degrades_to_no_signal():
    r = run(["zai", "openai-codex"],
            {"z-ai": obs("z-ai", used=99, limit=100, age_seconds=99999),
             "openai": obs("openai", used=1, limit=100)},
            max_age_seconds=3600)
    assert benched_names(r) == set()
    zai = next(a for a in r["assessments"] if a["pool_provider"] == "zai")
    assert "stale" in zai["skip_reason"]


def test_estimated_excluded_when_opted_out():
    r = run(["opencode-go", "openai-codex"],
            {"opencode-go": obs("opencode-go", used=29, limit=30, confidence="estimated"),
             "openai": obs("openai", used=1, limit=100)},
            include_estimated=False)
    assert benched_names(r) == set()


def test_five_hour_window_is_never_consulted():
    """Only weekly names are queried; a spent 5h window must not bench."""
    assert "five_hour" not in qb.WEEKLY_QUOTA_NAMES
    r = run(["zai"], {"z-ai": obs("z-ai", used=99, limit=100, quota_name="five_hour")})
    # evaluate() trusts its input, but the ledger query filters on WEEKLY_QUOTA_NAMES;
    # this asserts the constant that filter is built from.
    assert r["assessments"][0]["quota_name"] == "five_hour"


def test_ledger_provider_names_map_to_pool_names():
    assert qb.LEDGER_TO_POOL["z-ai"] == "zai"
    # wham/usage is the ChatGPT OAuth surface, not the separately-billed openai-api pool
    assert qb.LEDGER_TO_POOL["openai"] == "openai-codex"
    assert qb.POOL_TO_LEDGER["zai"] == "z-ai"


# ── per-provider thresholds (keeper ruling 2026-08-31) ──────────────────────
# opencode-go is estimated over a rolling window; benching it at 90% would be
# acting on a guess. It runs to the cap instead.


def test_opencode_go_defaults_to_the_cap_not_ninety():
    assert qb.DEFAULT_PROVIDER_THRESHOLDS["opencode-go"] == 100.0
    assert "zai" not in qb.DEFAULT_PROVIDER_THRESHOLDS
    assert "kimi-coding" not in qb.DEFAULT_PROVIDER_THRESHOLDS


def test_opencode_go_at_95_percent_is_not_benched():
    """95% would trip the global 90%, but opencode-go runs to its cap."""
    r = run(["opencode-go", "openai-codex"],
            {"opencode-go": obs("opencode-go", used=28.5, limit=30,
                                confidence="estimated", window_kind="rolling"),
             "openai": obs("openai", used=1, limit=100)},
            provider_thresholds=qb.DEFAULT_PROVIDER_THRESHOLDS)
    assert benched_names(r) == set()
    row = next(a for a in r["assessments"] if a["pool_provider"] == "opencode-go")
    assert row["threshold_pct"] == 100.0
    assert row["pct"] == pytest.approx(95.0)


def test_opencode_go_at_the_cap_is_benched():
    r = run(["opencode-go", "openai-codex"],
            {"opencode-go": obs("opencode-go", used=30, limit=30,
                                confidence="estimated", window_kind="rolling"),
             "openai": obs("openai", used=1, limit=100)},
            provider_thresholds=qb.DEFAULT_PROVIDER_THRESHOLDS)
    assert benched_names(r) == {"opencode-go"}
    assert r["benched"][0]["bench_basis"] == "rolling window, no resets_at"


def test_override_does_not_leak_to_other_providers():
    """zai must still bench at the global 90% while opencode-go waits for 100%."""
    r = run(["zai", "opencode-go", "openai-codex"],
            {"z-ai": obs("z-ai", used=92, limit=100),
             "opencode-go": obs("opencode-go", used=28.5, limit=30,
                                confidence="estimated", window_kind="rolling"),
             "openai": obs("openai", used=1, limit=100)},
            provider_thresholds=qb.DEFAULT_PROVIDER_THRESHOLDS)
    assert benched_names(r) == {"zai"}


def test_fail_open_respects_per_provider_thresholds():
    """Everything over *its own* threshold still fails open."""
    r = run(["zai", "opencode-go"],
            {"z-ai": obs("z-ai", used=95, limit=100),
             "opencode-go": obs("opencode-go", used=30, limit=30,
                                confidence="estimated", window_kind="rolling")},
            provider_thresholds=qb.DEFAULT_PROVIDER_THRESHOLDS)
    assert r["fail_open"] is True
    assert benched_names(r) == set()
