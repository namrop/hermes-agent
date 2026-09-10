"""Resume-after-interruption: policy, ledger query, window, and the real tick.

A gateway restart cuts every in-flight cron run. ``recover_interrupted_executions``
marks the abandoned attempts ``unknown`` "without scheduling retries", and the
job's ``next_run_at`` advanced at fire time, so the run is lost until the next
occurrence — a weekly watcher killed on Monday is silent for a week (nine jobs
at once on 2026-08-27; two more on 2026-09-09 07:30).

These tests cover the four pieces of the repair:
  * the per-job ``resume`` policy grammar and its defaults (cron/jobs.py),
  * ``find_resumable`` — which interrupted attempts the ledger will offer,
    including the one-rerun bound (cron/executions.py),
  * ``_resume_window_open`` — the schedule's own next occurrence as the bound,
  * ``tick`` end-to-end: an interrupted attempt is re-run once, recorded with
    ``resume_of``, and the job's schedule is not moved.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ── policy grammar + defaults ───────────────────────────────────────────────


def test_resume_policy_grammar():
    from cron.jobs import InvalidResumePolicy, normalize_resume_policy as n

    assert n("skip") == "skip"
    assert n("  RERUN_ONCE ") == "rerun_once"
    assert n("rerun_if_within_hours:6") == "rerun_if_within_hours:6"
    assert n("rerun_if_within_hours:1.5") == "rerun_if_within_hours:1.5"
    # None / "" mean "clear it, follow the default" — not "skip".
    assert n(None) is None
    assert n("") is None

    for bad in ("bogus", "rerun_if_within_hours:", "rerun_if_within_hours:0",
                "rerun_if_within_hours:-3", "rerun_if_within_hours:soon"):
        with pytest.raises(InvalidResumePolicy):
            n(bad)


def test_resume_defaults_are_opt_in_for_anything_that_delivers():
    """The only safe automatic rerun is a producer nobody hears from.

    A rerun of a delivering job posts a second message, and ``unknown`` means
    precisely that we cannot know whether the interrupted attempt already
    posted the first one.
    """
    from cron.jobs import default_resume_policy as d

    assert d({"no_agent": True, "deliver": None}) == "rerun_once"
    assert d({"no_agent": True, "deliver": ""}) == "rerun_once"
    assert d({"no_agent": True, "deliver": "none"}) == "rerun_once"
    # Delivering script job, agent job, plain job: all skip.
    assert d({"no_agent": True, "deliver": "discord:1:2"}) == "skip"
    assert d({"no_agent": True, "deliver": "origin"}) == "skip"
    assert d({"no_agent": False, "deliver": None}) == "skip"
    assert d({}) == "skip"


def test_effective_policy_prefers_the_explicit_setting_and_survives_garbage():
    from cron.jobs import effective_resume_policy as e

    assert e({"no_agent": True, "deliver": None, "resume": "skip"}) == "skip"
    assert e({"deliver": "origin", "resume": "rerun_once"}) == "rerun_once"
    # An unreadable stored value must fail safe, not crash the tick.
    assert e({"resume": "nonsense"}) == "skip"


# ── ledger: find_resumable ──────────────────────────────────────────────────


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    import cron.executions as E

    monkeypatch.setattr(E, "EXECUTIONS_FILE", tmp_path / "executions.db")
    return E


def _row(E, job_id, status, claimed_at, *, resume_of=None, exec_id=None):
    """Insert an execution row directly, at a chosen time and status."""
    import uuid

    exec_id = exec_id or uuid.uuid4().hex
    with E._transaction() as conn:
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at,
                status, claimed_at, resume_of)
               VALUES (?, ?, 'test', 'proc', 1, NULL, ?, ?, ?)""",
            (exec_id, job_id, status, claimed_at, resume_of),
        )
    return exec_id


def test_find_resumable_returns_an_orphaned_attempt(ledger):
    lost = _row(ledger, "job-a", "unknown", "2026-09-09T07:30:00-04:00")
    found = ledger.find_resumable()
    assert [r["id"] for r in found] == [lost]


def test_find_resumable_skips_a_job_that_has_since_succeeded(ledger):
    _row(ledger, "job-a", "unknown", "2026-09-09T07:30:00-04:00")
    _row(ledger, "job-a", "completed", "2026-09-09T08:30:00-04:00")
    assert ledger.find_resumable() == []


def test_find_resumable_refuses_to_resume_twice(ledger):
    """The bound: one rerun per interruption, recorded by resume_of."""
    lost = _row(ledger, "job-a", "unknown", "2026-09-09T07:30:00-04:00")
    # The rerun happened and itself failed. Neither row may be offered again:
    # the original is already resumed, and the rerun IS a resume.
    _row(ledger, "job-a", "failed", "2026-09-09T07:40:00-04:00", resume_of=lost)
    assert ledger.find_resumable() == []


def test_find_resumable_offers_only_the_newest_loss_per_job(ledger):
    """A job cut three weeks running gets one rerun, not three."""
    _row(ledger, "job-a", "unknown", "2026-08-20T07:30:00-04:00")
    _row(ledger, "job-a", "unknown", "2026-08-27T07:30:00-04:00")
    newest = _row(ledger, "job-a", "unknown", "2026-09-03T07:30:00-04:00")
    assert [r["id"] for r in ledger.find_resumable()] == [newest]


def test_find_resumable_stands_down_while_a_run_is_in_flight(ledger):
    """A live attempt is the recovery; don't race it."""
    _row(ledger, "job-a", "unknown", "2026-09-09T07:30:00-04:00")
    _row(ledger, "job-a", "running", "2026-09-09T09:00:00-04:00")
    assert ledger.find_resumable() == []


def test_find_resumable_orders_oldest_first(ledger):
    b = _row(ledger, "job-b", "unknown", "2026-09-08T07:30:00-04:00")
    a = _row(ledger, "job-a", "failed", "2026-09-09T07:30:00-04:00")
    assert [r["id"] for r in ledger.find_resumable()] == [b, a]


def test_resume_of_column_is_added_to_a_pre_existing_ledger(tmp_path, monkeypatch):
    """The live executions.db predates the column; CREATE TABLE IF NOT EXISTS
    would never add it. The migration must be idempotent on a real file."""
    import cron.executions as E

    db = tmp_path / "executions.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE executions (
             id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
             process_id TEXT NOT NULL, pid INTEGER NOT NULL,
             process_started_at INTEGER,
             status TEXT NOT NULL CHECK(status IN
               ('claimed','running','completed','failed','unknown')),
             claimed_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
             error TEXT)"""
    )
    conn.execute(
        "INSERT INTO executions VALUES ('old','job-a','builtin','p',1,NULL,"
        "'completed','2026-09-01T00:00:00-04:00',NULL,NULL,NULL)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(E, "EXECUTIONS_FILE", db)
    # Two opens: the second must not fail on a duplicate ALTER.
    assert E.list_executions()[0]["resume_of"] is None
    assert E.list_executions()[0]["id"] == "old"


# ── window ──────────────────────────────────────────────────────────────────


def test_resume_window_is_the_schedules_own_next_occurrence():
    from cron.scheduler import _resume_window_open

    now = datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc)
    execution = {"claimed_at": (now - timedelta(hours=1)).isoformat()}
    open_job = {"next_run_at": (now + timedelta(hours=23)).isoformat()}
    closed_job = {"next_run_at": (now - timedelta(minutes=1)).isoformat()}

    assert _resume_window_open(open_job, execution, "rerun_once", now) is True
    # The job has already come round again: the fresh run supersedes the loss.
    assert _resume_window_open(closed_job, execution, "rerun_once", now) is False
    # No schedule to bound it: refuse.
    assert _resume_window_open({}, execution, "rerun_once", now) is False


def test_rerun_if_within_hours_tightens_the_window():
    from cron.scheduler import _resume_window_open

    now = datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc)
    job = {"next_run_at": (now + timedelta(hours=23)).isoformat()}
    recent = {"claimed_at": (now - timedelta(hours=1)).isoformat()}
    stale = {"claimed_at": (now - timedelta(hours=9)).isoformat()}

    assert _resume_window_open(job, recent, "rerun_if_within_hours:4", now) is True
    assert _resume_window_open(job, stale, "rerun_if_within_hours:4", now) is False
    # Same stale attempt is fine under the unbounded policy.
    assert _resume_window_open(job, stale, "rerun_once", now) is True


# ── end-to-end through the real tick ────────────────────────────────────────


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    """Throwaway HERMES_HOME with a no_agent cron job and a working script."""
    import cron.executions as E
    import cron.jobs as J
    import cron.scheduler as S

    home = tmp_path / ".hermes"
    (home / "cron" / "output").mkdir(parents=True)
    (home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(J, "HERMES_DIR", home)
    monkeypatch.setattr(J, "CRON_DIR", home / "cron")
    monkeypatch.setattr(J, "JOBS_FILE", home / "cron" / "jobs.json")
    monkeypatch.setattr(J, "OUTPUT_DIR", home / "cron" / "output")
    monkeypatch.setattr(E, "EXECUTIONS_FILE", home / "cron" / "executions.db")
    monkeypatch.setattr(S, "_hermes_home", home)
    # Force the throttled reap/resume pass to run on the next tick.
    monkeypatch.setattr(S, "_last_dead_owner_reap_at", None, raising=False)

    (home / "scripts" / "probe.py").write_text("print('ledger row written')\n")
    return home


def _make_job(schedule="0 8 * * *", **overrides):
    import cron.jobs as J

    job = J.create_job(
        prompt="probe", schedule=schedule, no_agent=True, script="probe.py"
    )
    if overrides:
        J.update_job(job["id"], overrides)
    return J.get_job(job["id"])


def _orphan_an_attempt(job_id):
    """Record an attempt, then let the real reap classify it as a restart loss.

    The status is never faked: ``recover_interrupted_executions`` only rewrites
    rows whose exact owner process is proved gone, so the test models the dead
    owner and runs the real recovery. The patches are scoped so the resume's
    own execution row is not also treated as orphaned.
    """
    import cron.executions as E

    execution = E.create_execution(job_id, source="builtin")
    E.mark_execution_running(execution["id"])
    with pytest.MonkeyPatch.context() as m:
        m.setattr(E, "_PROCESS_ID", "a-previous-gateway-process")
        m.setattr(E, "_owner_is_live", lambda pid, started_at: False)
        assert E.recover_interrupted_executions() >= 1
    assert E.latest_execution(job_id)["status"] == "unknown"
    return execution["id"]


def test_interrupted_run_is_resumed_once_without_moving_the_schedule(
    cron_env, monkeypatch
):
    import cron.executions as E
    import cron.jobs as J
    import cron.scheduler as S

    job = _make_job(resume="rerun_once")
    # Not due: next_run_at is tomorrow morning, which is also the resume window.
    future = datetime.now(timezone.utc) + timedelta(hours=12)
    J.update_job(job["id"], {"next_run_at": future.isoformat()})
    schedule_before = J.get_job(job["id"])["schedule"]

    lost = _orphan_an_attempt(job["id"])

    assert S.tick(verbose=False, sync=True) == 1

    rows = E.list_executions(job_id=job["id"], limit=10)
    resumed = [r for r in rows if r.get("resume_of")]
    assert len(resumed) == 1, f"expected exactly one resume, got {rows}"
    assert resumed[0]["resume_of"] == lost
    assert resumed[0]["source"] == "resume"
    assert resumed[0]["status"] == "completed"
    # The interrupted attempt kept its own terminal record.
    assert [r for r in rows if r["id"] == lost][0]["status"] == "unknown"

    after = J.get_job(job["id"])
    assert after["schedule"] == schedule_before
    # mark_job_run recomputed next_run_at from the cron expression, so it is
    # still the schedule's own next occurrence — the resume moved nothing.
    assert after["next_run_at"] == J.compute_next_run(
        after["schedule"], datetime.now(timezone.utc).isoformat()
    )


def test_resume_happens_at_most_once(cron_env, monkeypatch):
    """Second tick must not resume the same loss again."""
    import cron.executions as E
    import cron.jobs as J
    import cron.scheduler as S

    job = _make_job(resume="rerun_once")
    future = datetime.now(timezone.utc) + timedelta(hours=12)
    J.update_job(job["id"], {"next_run_at": future.isoformat()})
    _orphan_an_attempt(job["id"])

    S.tick(verbose=False, sync=True)
    J.update_job(job["id"], {"next_run_at": future.isoformat()})
    monkeypatch.setattr(S, "_last_dead_owner_reap_at", None, raising=False)
    assert S.tick(verbose=False, sync=True) == 0

    resumed = [
        r for r in E.list_executions(job_id=job["id"], limit=20) if r.get("resume_of")
    ]
    assert len(resumed) == 1


def test_skip_policy_leaves_the_loss_alone(cron_env, monkeypatch):
    import cron.executions as E
    import cron.jobs as J
    import cron.scheduler as S

    job = _make_job(resume="skip")
    future = datetime.now(timezone.utc) + timedelta(hours=12)
    J.update_job(job["id"], {"next_run_at": future.isoformat()})
    _orphan_an_attempt(job["id"])

    assert S.tick(verbose=False, sync=True) == 0
    assert not [
        r for r in E.list_executions(job_id=job["id"], limit=20) if r.get("resume_of")
    ]


def test_interval_jobs_are_never_resumed(cron_env, monkeypatch):
    """claim_job_for_fire and mark_job_run both RE-ANCHOR an interval schedule
    from the run time, so resuming one would silently move it."""
    import cron.jobs as J
    import cron.scheduler as S

    job = _make_job(schedule="every 6h", resume="rerun_once")
    future = datetime.now(timezone.utc) + timedelta(hours=5)
    J.update_job(job["id"], {"next_run_at": future.isoformat()})
    _orphan_an_attempt(job["id"])

    assert S._collect_resume_jobs() == []


def test_paused_job_is_not_resumed(cron_env, monkeypatch):
    import cron.jobs as J
    import cron.scheduler as S

    job = _make_job(resume="rerun_once")
    future = datetime.now(timezone.utc) + timedelta(hours=12)
    J.update_job(job["id"], {"next_run_at": future.isoformat()})
    _orphan_an_attempt(job["id"])
    J.pause_job(job["id"], "keeper")

    assert S._collect_resume_jobs() == []


def test_resume_is_bounded_to_one_job_per_tick(cron_env, monkeypatch):
    """The 2026-08-27 shape: an init stall killed nine jobs at once. Firing
    all of them back into a freshly-started gateway is the same stall."""
    import cron.scheduler as S

    future = datetime.now(timezone.utc) + timedelta(hours=12)
    import cron.jobs as J

    for _ in range(3):
        job = _make_job(resume="rerun_once")
        J.update_job(job["id"], {"next_run_at": future.isoformat()})
        _orphan_an_attempt(job["id"])

    assert S._MAX_RESUMES_PER_TICK == 1
    assert len(S._collect_resume_jobs()) == 1


def test_a_due_job_is_not_also_resumed(cron_env, monkeypatch):
    """The due fire IS the recovery — dispatching both would double-run it."""
    import cron.executions as E
    import cron.jobs as J
    import cron.scheduler as S

    job = _make_job(resume="rerun_once")
    J.update_job(
        job["id"],
        {"next_run_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()},
    )
    _orphan_an_attempt(job["id"])

    assert S.tick(verbose=False, sync=True) == 1
    assert not [
        r for r in E.list_executions(job_id=job["id"], limit=20) if r.get("resume_of")
    ]
