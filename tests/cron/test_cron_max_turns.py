"""Per-job max_turns override: store contract + scheduler precedence.

A cron job may pin its own tool-calling iteration cap, independent of the
global ``agent.max_turns``. Contract under test:

- Job store (cron/jobs.py): the field is validated at the storage choke
  point with the SAME parser the global knob uses
  (hermes_cli.config.resolve_turn_limit). Positive ints persist as ints,
  "unlimited" spellings persist as the canonical string "unlimited",
  garbage never persists, and an absent field keeps the record shape
  byte-identical to pre-feature jobs.
- Scheduler resolution (cron/scheduler.py::_resolve_job_max_iterations):
  a job pin wins outright over agent.max_turns; an absent pin yields the
  same value resolve_turn_limit(agent.max_turns) would; a garbage value in a
  hand-edited store warns and falls back to config instead of becoming
  unlimited.

Motivation: the newspaper composer job (2026-09-10) was cut off by the
global 180-turn cap before its publish and commit steps.
"""

import sys

import pytest

from cron.jobs import _normalize_max_turns, create_job, load_jobs, update_job
from cron.scheduler import _resolve_job_max_iterations


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Isolate the cron store (same pattern as tests/cron/test_jobs.py)."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path / "cron"


def _create(**kw):
    kw.setdefault("prompt", "say hi")
    kw.setdefault("schedule", "every 1h")
    return create_job(**kw)


class TestNormalizeMaxTurns:
    @pytest.mark.parametrize("raw,expected", [
        (400, 400),
        ("400", 400),
        (" 12 ", 12),
        (2.9, 2),
    ])
    def test_positive_values_become_ints(self, raw, expected):
        assert _normalize_max_turns(raw) == expected

    @pytest.mark.parametrize("raw", ["none", "unlimited", "None", " inf ", 0, -1, "0", "-1"])
    def test_unlimited_spellings_canonicalize(self, raw):
        assert _normalize_max_turns(raw) == "unlimited"

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_unset_is_none(self, raw):
        assert _normalize_max_turns(raw) is None

    @pytest.mark.parametrize("raw", ["lots", "12x", True, False, [400]])
    def test_garbage_raises(self, raw):
        with pytest.raises(ValueError):
            _normalize_max_turns(raw)


class TestJobStoreMaxTurns:
    def test_absent_field_keeps_shape(self, tmp_cron_dir):
        job = _create()
        assert "max_turns" not in job
        assert job.get("reasoning_effort") is None

    def test_int_pin_round_trips(self, tmp_cron_dir):
        job = _create(max_turns="400")
        assert job["max_turns"] == 400
        assert load_jobs()[0]["max_turns"] == 400

    def test_unlimited_pin_round_trips(self, tmp_cron_dir):
        job = _create(max_turns="unlimited")
        assert job["max_turns"] == "unlimited"
        assert load_jobs()[0]["max_turns"] == "unlimited"

    def test_garbage_never_persists(self, tmp_cron_dir):
        with pytest.raises(ValueError):
            _create(max_turns="a lot")
        assert load_jobs() == []

    def test_update_sets_and_clears(self, tmp_cron_dir):
        job = _create()
        updated = update_job(job["id"], {"max_turns": "250"})
        assert updated["max_turns"] == 250
        cleared = update_job(job["id"], {"max_turns": ""})
        assert cleared["max_turns"] is None

    def test_update_garbage_leaves_store_untouched(self, tmp_cron_dir):
        job = _create(max_turns=300)
        with pytest.raises(ValueError):
            update_job(job["id"], {"max_turns": "nope"})
        assert load_jobs()[0]["max_turns"] == 300


class TestSchedulerResolution:
    def test_absent_pin_follows_agent_max_turns(self):
        assert _resolve_job_max_iterations({"id": "j"}, {"agent": {"max_turns": 180}}) == 180

    def test_absent_pin_and_absent_config_is_unlimited(self):
        assert _resolve_job_max_iterations({"id": "j"}, {}) == sys.maxsize

    def test_absent_pin_uses_legacy_top_level_key(self):
        assert _resolve_job_max_iterations({"id": "j"}, {"max_turns": 90}) == 90

    def test_pin_wins_over_config(self):
        assert _resolve_job_max_iterations({"id": "j", "max_turns": 400}, {"agent": {"max_turns": 180}}) == 400

    def test_unlimited_pin_wins_over_config(self):
        assert _resolve_job_max_iterations({"id": "j", "max_turns": "unlimited"}, {"agent": {"max_turns": 180}}) == sys.maxsize

    def test_numeric_string_pin_from_hand_edit(self):
        assert _resolve_job_max_iterations({"id": "j", "max_turns": "250"}, {"agent": {"max_turns": 180}}) == 250

    def test_garbage_pin_warns_and_falls_back_to_config(self, caplog):
        with caplog.at_level("WARNING", logger="cron.scheduler"):
            got = _resolve_job_max_iterations({"id": "j", "max_turns": "lots"}, {"agent": {"max_turns": 180}})
        assert got == 180
        assert "invalid stored max_turns" in caplog.text
