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


# ── un-bench (2026-09-12) ────────────────────────────────────────────────────
# A bench this script wrote is only kept while the observation that justified
# it still holds. openai-codex was benched 09-10 to the Thursday cliff; the
# week rolled 09-12 05:00 EDT (0% used, cliff 09-19) and seventeen hourly runs
# printed "openai-codex 1.0% under" without lifting it.

import json
import os
import sqlite3


def bench_entry(provider, *, cliff, message=None, basis="resets_at", status="exhausted",
                entry_id=None, label=None):
    return {
        "id": entry_id or f"{provider}-1",
        "label": label or f"{provider.upper()}_KEY",
        "last_status": status,
        "last_status_at": NOW - 3600,
        "last_error_code": 429,
        "last_error_reason": None,
        "last_error_message": message if message is not None else
            "pre-emptive quota bench at 90.0% of weekly week (quota_bench.py)",
        "last_error_reset_at": cliff,
        "failure_reason": "rate_limit",
        "bench_basis": basis,
    }


def reactive_entry(provider, *, cliff=None):
    """A 403/429 the chat path marked — not ours."""
    return {
        "id": f"{provider}-1",
        "label": f"{provider.upper()}_KEY",
        "last_status": "exhausted",
        "last_status_at": NOW - 60,
        "last_error_code": 403,
        "last_error_reason": "weekly_limit",
        "last_error_message": "Weekly usage limit reached. Resets in 2 days.",
        "last_error_reset_at": cliff,
        "failure_reason": "rate_limit",
        "bench_basis": None,
    }


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + "Z"


def unbench(chain, observations, pool_status, **kw):
    result = run(chain, observations, **kw)
    rows = qb.evaluate_unbench(
        result["assessments"], pool_status, now=NOW,
        rebench={r["pool_provider"] for r in result["benched"]},
    )
    return result, rows


def test_is_bench_entry_recognises_marker_and_legacy_message():
    assert qb.is_bench_entry(bench_entry("zai", cliff=NOW + 100)) is True
    legacy = bench_entry("zai", cliff=NOW + 100, basis=None)
    assert qb.is_bench_entry(legacy) is True
    hybrid = bench_entry("zai", cliff=NOW + 100, message="Weekly usage limit reached.")
    assert qb.is_bench_entry(hybrid) is True, "a reactive 403 on a benched entry keeps bench_basis"
    assert qb.is_bench_entry(reactive_entry("zai")) is False
    assert qb.is_bench_entry(bench_entry("zai", cliff=NOW + 100, status=None)) is False


def test_codex_case_under_threshold_after_the_week_rolled_is_lifted():
    """1% used, new cliff a week out; the entry still holds Thursday's cliff."""
    thursday = NOW + 2 * 86400
    _, rows = unbench(
        ["kimi-coding", "openai-codex"],
        {"kimi-coding": obs("kimi-coding", used=10, limit=100),
         "openai": obs("openai", used=1, limit=100, resets_at=iso(NOW + 7 * 86400))},
        {"openai-codex": [bench_entry("openai-codex", cliff=thursday)]},
    )
    assert [(r["pool_provider"], r["action"]) for r in rows] == [("openai-codex", "unbench")]
    assert "under threshold: 1.0% < 90%" in rows[0]["reason"]
    assert rows[0]["stored_cliff"] == thursday


def test_kimi_case_expired_cliff_and_zero_used_is_lifted():
    _, rows = unbench(
        ["kimi-coding", "zai"],
        {"kimi-coding": obs("kimi-coding", used=0, limit=100, resets_at=iso(NOW + 7 * 86400)),
         "z-ai": obs("z-ai", used=41, limit=100)},
        {"kimi-coding": [bench_entry("kimi-coding", cliff=NOW - 900, basis=None)]},
    )
    assert rows[0]["action"] == "unbench"


def test_window_rolled_lifts_even_when_still_over_and_not_rebenched():
    """Fail-open run: still over in the NEW window, nothing re-benched, and
    the entry holds the OLD window's cliff — the cliff is stale, lift it."""
    old_cliff, new_cliff = NOW + 3600, NOW + 7 * 86400
    result, rows = unbench(
        ["zai", "openai-codex"],
        {"z-ai": obs("z-ai", used=99, limit=100),
         "openai": obs("openai", used=95, limit=100, resets_at=iso(new_cliff))},
        {"openai-codex": [bench_entry("openai-codex", cliff=old_cliff)]},
    )
    assert result["fail_open"] is True
    assert rows[0]["action"] == "unbench"
    assert rows[0]["reason"].startswith("window rolled")


def test_rebenched_provider_is_kept_not_churned():
    cliff = NOW + 3600
    _, rows = unbench(
        ["zai", "openai-codex"],
        {"z-ai": obs("z-ai", used=95, limit=100, resets_at=iso(NOW + 7 * 86400)),
         "openai": obs("openai", used=1, limit=100)},
        {"zai": [bench_entry("zai", cliff=cliff)]},
    )
    assert rows[0]["action"] == "keep"
    assert "re-benched" in rows[0]["reason"]


def test_no_fresh_signal_keeps_the_bench():
    _, rows = unbench(
        ["zai", "openai-codex"],
        {"z-ai": obs("z-ai", used=1, limit=100, age_seconds=99999),
         "openai": obs("openai", used=1, limit=100)},
        {"zai": [bench_entry("zai", cliff=NOW + 3600)]},
        max_age_seconds=3600,
    )
    assert rows[0]["action"] == "keep"
    assert "no fresh signal" in rows[0]["reason"]
    assert "stale" in rows[0]["reason"]


def test_still_over_in_the_same_window_is_kept():
    cliff = NOW + 3600
    _, rows = unbench(
        ["zai", "openai-codex"],
        {"z-ai": obs("z-ai", used=95, limit=100, resets_at=iso(cliff + 5)),
         "openai": obs("openai", used=95, limit=100)},   # fail open -> no rebench
        {"zai": [bench_entry("zai", cliff=cliff)]},
    )
    assert rows[0]["action"] == "keep"
    assert "same window" in rows[0]["reason"]


def test_reactive_marking_is_never_lifted():
    _, rows = unbench(
        ["zai", "openai-codex"],
        {"z-ai": obs("z-ai", used=1, limit=100),
         "openai": obs("openai", used=1, limit=100)},
        {"zai": [reactive_entry("zai", cliff=NOW + 3600)]},
    )
    assert rows == []


def test_evaluate_rows_carry_parsed_resets_at():
    r = run(["zai"], {"z-ai": obs("z-ai", used=1, limit=100, resets_at=iso(NOW + 10))})
    assert r["assessments"][0]["resets_at"] == pytest.approx(NOW + 10, abs=1)


# ── pool I/O ─────────────────────────────────────────────────────────────────


def _hermes_home(tmp_path, monkeypatch, pool):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": pool}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_cli.auth._import_codex_cli_tokens", lambda: None)
    return home


def _manual(provider, **fields):
    entry = {
        "id": f"{provider}-1", "label": f"{provider}-key", "auth_type": "api_key",
        "priority": 0, "source": "manual", "access_token": f"sk-{provider}-secret",
    }
    entry.update(fields)
    return entry


def test_read_pool_status_lifts_status_fields_only(tmp_path, monkeypatch):
    home = _hermes_home(tmp_path, monkeypatch, {
        "kimi-coding": [_manual("kimi-coding", **bench_entry("kimi-coding", cliff=NOW + 10))],
    })
    status = qb.read_pool_status(home)
    entry = status["kimi-coding"][0]
    assert entry["last_status"] == "exhausted"
    assert entry["bench_basis"] == "resets_at"
    assert "access_token" not in entry
    assert "sk-kimi-coding-secret" not in json.dumps(status)
    assert qb.read_pool_status(tmp_path / "nowhere") == {}


def test_apply_unbenches_clears_only_the_bench(tmp_path, monkeypatch):
    home = _hermes_home(tmp_path, monkeypatch, {
        "kimi-coding": [_manual("kimi-coding",
                                **bench_entry("kimi-coding", cliff=time.time() + 3600))],
        "zai": [_manual("zai", **reactive_entry("zai", cliff=time.time() + 3600))],
    })
    rows = [{"pool_provider": "kimi-coding", "entry_id": "kimi-coding-1",
             "label": "kimi-coding-key", "reason": "under threshold: 0.0% < 90%"}]

    assert qb.apply_unbenches(rows, hermes_home=home, verbose=False) == 1

    pool = json.loads((home / "auth.json").read_text())["credential_pool"]
    kimi = pool["kimi-coding"][0]
    assert kimi["last_status"] is None
    assert kimi["last_error_reset_at"] is None
    assert kimi["last_error_code"] is None
    assert "failure_reason" not in kimi and "bench_basis" not in kimi
    assert kimi["last_error_message"].startswith("quota bench cleared ")
    assert "under threshold" in kimi["last_error_message"]
    assert kimi["last_status_at"] > time.time() - 60, "the clear is a timestamped event"
    assert pool["zai"][0]["last_status"] == "exhausted", "reactive marking untouched"


def test_apply_benches_stamps_bench_basis(tmp_path, monkeypatch):
    home = _hermes_home(tmp_path, monkeypatch, {"zai": [_manual("zai")]})
    row = {"pool_provider": "zai", "pct": 92.0, "quota_name": "week",
           "bench_until": time.time() + 3600, "bench_basis": "resets_at"}

    assert qb.apply_benches([row], hermes_home=home, verbose=False) == 1

    entry = json.loads((home / "auth.json").read_text())["credential_pool"]["zai"][0]
    assert entry["last_status"] == "exhausted"
    assert entry["bench_basis"] == "resets_at"
    assert qb.BENCH_MESSAGE_MARKER in entry["last_error_message"]
    assert qb.is_bench_entry(entry) is True


# ── main(): report and write discipline ──────────────────────────────────────


def _ledger(tmp_path, rows):
    path = tmp_path / "quota_observations.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE facts (canonical_json TEXT, provider TEXT, "
                 "quota_name TEXT, occurred_or_observed_at TEXT)")
    for rec in rows:
        conn.execute("INSERT INTO facts VALUES (?, ?, ?, ?)",
                     (json.dumps(rec), rec["provider"], rec["quota_name"], rec["observed_at"]))
    conn.commit()
    conn.close()
    return path


def _live_obs(provider, *, used, resets_at):
    """A fresh observation against the wall clock (main() uses time.time())."""
    now = time.time()
    return {
        "provider": provider, "quota_name": "week", "used_value": str(used),
        "limit_value": "100", "unit": "percent", "measurement_confidence": "exact",
        "observed_at": iso(now - 30), "resets_at": iso(resets_at), "window_kind": "fixed",
    }


def _main_home(tmp_path, monkeypatch):
    home = _hermes_home(tmp_path, monkeypatch, {
        "kimi-coding": [_manual("kimi-coding",
                                **bench_entry("kimi-coding", cliff=time.time() - 900, basis=None))],
    })
    (home / "config.yaml").write_text(
        "model:\n  provider: kimi-coding\nfallback_providers:\n  - provider: zai\n")
    ledger = _ledger(tmp_path, [
        _live_obs("kimi-coding", used=0, resets_at=time.time() + 7 * 86400),
        _live_obs("z-ai", used=41, resets_at=time.time() + 4 * 86400),
    ])
    return home, ledger


def test_main_dry_run_reports_the_lift_and_writes_nothing(tmp_path, monkeypatch, capsys):
    home, ledger = _main_home(tmp_path, monkeypatch)
    before = (home / "auth.json").read_bytes()

    assert qb.main(["--hermes-home", str(home), "--ledger", str(ledger)]) == 0

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "would lift: under threshold: 0.0% < 90%" in out
    assert "Would un-bench: kimi-coding (KIMI-CODING_KEY)" in out
    assert "No provider is over threshold" in out
    assert (home / "auth.json").read_bytes() == before


def test_main_apply_lifts_the_bench(tmp_path, monkeypatch, capsys):
    home, ledger = _main_home(tmp_path, monkeypatch)

    assert qb.main(["--hermes-home", str(home), "--ledger", str(ledger), "--apply"]) == 0

    out = capsys.readouterr().out
    assert "Un-benching: kimi-coding (KIMI-CODING_KEY)" in out
    assert "✔ un-benched kimi-coding" in out
    assert "applied=0 unbenched=1" in out
    entry = json.loads((home / "auth.json").read_text())["credential_pool"]["kimi-coding"][0]
    assert entry["last_status"] is None
    assert entry["last_error_reset_at"] is None


def test_main_json_includes_unbench_rows(tmp_path, monkeypatch, capsys):
    home, ledger = _main_home(tmp_path, monkeypatch)
    assert qb.main(["--hermes-home", str(home), "--ledger", str(ledger), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert [r["action"] for r in payload["unbench"]] == ["unbench"]
