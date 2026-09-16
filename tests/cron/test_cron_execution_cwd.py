"""Per-execution working directories for cron jobs.

Behaviour contract (backport of upstream b7c59bd + 62b0235):

* A cron job's ``workdir`` is bound to the fire's own task identity
  (``cron:<job id>:<execution id>``), not to the process-global
  ``TERMINAL_CWD`` env var and not behind a scheduler-wide lock.
* Two jobs with *different* workdirs, and a workdir-less job beside them,
  therefore all run at the same time and each sees only its own directory
  through the tool layer (terminal / file / execute_code).
* The record is stable for the whole run and is dropped when the run ends,
  including when the run raises.
* Pre-run scripts for agent jobs run in the configured workdir.

These assert observable behaviour through the real resolver functions the
tools call; nothing here reads source text.
"""

from __future__ import annotations

import os
import sys
import threading

import pytest


# ---------------------------------------------------------------------------
# Shared stubs — enough of run_job's dependencies that it executes with no
# credentials, no network, and no real AIAgent.
# ---------------------------------------------------------------------------


def _install_stubs(monkeypatch, agent_cls):
    import cron.scheduler as sched

    fake_mod = type(sys)("run_agent")
    fake_mod.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_mod)

    from hermes_cli import runtime_provider as _rtp

    monkeypatch.setattr(
        _rtp,
        "resolve_runtime_provider",
        lambda **_kw: {
            "provider": "test",
            "api_key": "k",
            "base_url": "http://test.local",
            "api_mode": "chat_completions",
        },
    )

    monkeypatch.setattr(
        sched, "_build_job_prompt", lambda job, prerun_script=None, **kw: "hi"
    )
    monkeypatch.setattr(sched, "_resolve_origin", lambda job: None)
    monkeypatch.setattr(sched, "_resolve_delivery_target", lambda job: None)
    monkeypatch.setattr(sched, "_resolve_cron_enabled_toolsets", lambda job, cfg: None)
    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")

    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_a, **_kw: True)


def _observe_tool_layer(task_id):
    """What the three cwd-consuming tool surfaces resolve for *task_id*.

    These are the real resolvers ``terminal``, the file tools and
    ``execute_code`` call, so a passing assertion means the tools themselves
    would run in that directory.
    """
    from tools.code_execution_tool import _resolve_child_cwd
    from tools.file_tools import _resolve_base_dir
    from tools.terminal_tool import _resolve_command_cwd, get_session_cwd

    return {
        "record": get_session_cwd(task_id),
        "terminal": _resolve_command_cwd(
            workdir=None, default_cwd="/default-cwd", session_key=task_id
        ),
        "file": str(_resolve_base_dir(task_id)),
        "execute_code": _resolve_child_cwd("project", "/staging", task_id or ""),
    }


class _RecordingAgent:
    """AIAgent stand-in that snapshots per-execution cwd state."""

    # Set by each test before run_job is called.
    sink: dict = {}
    barrier: threading.Barrier | None = None

    def __init__(self, **kwargs):
        self._skip_context_files = kwargs.get("skip_context_files")
        self._session_id = kwargs.get("session_id")

    def run_conversation(self, *_a, task_id=None, **_kw):
        from agent.runtime_cwd import resolve_context_cwd

        entry = {
            "task_id": task_id,
            "skip_context_files": self._skip_context_files,
            "terminal_cwd_env": os.environ.get("TERMINAL_CWD", "_UNSET_"),
            "context_cwd": str(resolve_context_cwd() or ""),
            "first": _observe_tool_layer(task_id),
        }
        type(self).sink[self._session_id] = entry
        if type(self).barrier is not None:
            type(self).barrier.wait()
        # Re-read after the overlap window: the record must be the same one
        # this run started with, not another concurrent run's.
        entry["second"] = _observe_tool_layer(task_id)
        return {"final_response": "done", "messages": []}

    def get_activity_summary(self):
        return {"seconds_since_activity": 0.0}


@pytest.fixture()
def recording_agent(monkeypatch):
    _RecordingAgent.sink = {}
    _RecordingAgent.barrier = None
    _install_stubs(monkeypatch, _RecordingAgent)
    yield _RecordingAgent
    _RecordingAgent.sink = {}
    _RecordingAgent.barrier = None


def _job(job_id, workdir=None, **extra):
    job = {
        "id": job_id,
        "name": job_id,
        "prompt": "hi",
        "schedule_display": "manual",
        "workdir": str(workdir) if workdir else None,
    }
    job.update(extra)
    return job


# ---------------------------------------------------------------------------
# Concurrency: distinct workdirs coexist, and nothing touches the process env
# ---------------------------------------------------------------------------


def test_distinct_workdirs_and_a_default_run_all_overlap(
    recording_agent, monkeypatch, tmp_path
):
    """The incident, inverted.

    Job A (/a), job B (/b) and a workdir-less job run at the same instant.
    Each must observe only its own directory, none may wait on another, and
    the process-global TERMINAL_CWD must be untouched throughout.
    """
    import cron.scheduler as sched

    baseline = tmp_path / "baseline"
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    for d in (baseline, dir_a, dir_b):
        d.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(baseline))

    # All three must be inside run_conversation simultaneously or this breaks.
    recording_agent.barrier = threading.Barrier(3, timeout=10)

    results: dict = {}
    jobs = [
        _job("wd-a", dir_a),
        _job("wd-b", dir_b),
        _job("no-wd", None),
    ]

    def run(job):
        results[job["id"]] = sched.run_job(job)

    threads = [threading.Thread(target=run, args=(job,)) for job in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    for job_id in ("wd-a", "wd-b", "no-wd"):
        assert results[job_id][0] is True, (
            f"{job_id} did not succeed: {results[job_id][3]!r}"
        )

    observed = {
        entry["task_id"].split(":")[1]: entry
        for entry in recording_agent.sink.values()
    }
    assert set(observed) == {"wd-a", "wd-b", "no-wd"}

    a, b, plain = observed["wd-a"], observed["wd-b"], observed["no-wd"]

    # Per-execution identity, unique per fire.
    assert a["task_id"].startswith("cron:wd-a:")
    assert b["task_id"].startswith("cron:wd-b:")
    assert a["task_id"] != b["task_id"]

    # Each workdir run resolves its own directory on every tool surface,
    # and still resolves the same one after the overlap window.
    for entry, want in ((a, dir_a), (b, dir_b)):
        for phase in ("first", "second"):
            assert entry[phase]["record"] == str(want)
            assert entry[phase]["terminal"] == str(want)
            assert entry[phase]["file"] == str(want)
            assert entry[phase]["execute_code"] == str(want)
        assert entry["context_cwd"] == str(want)
        assert entry["skip_context_files"] is False

    # The workdir-less run keeps the pre-existing, ambient semantics: no
    # per-task record, no context files, and it never sees another job's dir.
    assert plain["first"]["record"] is None
    assert plain["first"]["terminal"] == "/default-cwd"
    assert plain["first"]["file"] == str(baseline)
    assert plain["skip_context_files"] is True
    assert plain["context_cwd"] == str(baseline)

    # No process env mutation, during or after.
    for entry in (a, b, plain):
        assert entry["terminal_cwd_env"] == str(baseline)
    assert os.environ["TERMINAL_CWD"] == str(baseline)


def test_task_cwd_record_is_dropped_when_the_run_ends(
    recording_agent, monkeypatch, tmp_path
):
    import cron.scheduler as sched
    from tools.terminal_tool import get_session_cwd

    workdir = tmp_path / "project"
    workdir.mkdir()

    success, *_ = sched.run_job(_job("cleanup-ok", workdir))
    assert success is True

    entry = next(iter(recording_agent.sink.values()))
    assert entry["first"]["record"] == str(workdir)
    assert get_session_cwd(entry["task_id"]) is None


def test_task_cwd_record_is_dropped_when_the_run_raises_and_a_rerun_is_clean(
    monkeypatch, tmp_path
):
    """Cleanup on error, then reuse: a failed fire must not strand its record."""
    import cron.scheduler as sched
    from tools.terminal_tool import get_session_cwd

    workdir = tmp_path / "boom"
    workdir.mkdir()
    seen: list = []

    class BoomAgent(_RecordingAgent):
        def run_conversation(self, *_a, task_id=None, **_kw):
            seen.append((task_id, get_session_cwd(task_id)))
            raise RuntimeError("boom")

    _install_stubs(monkeypatch, BoomAgent)

    success, _out, _final, error = sched.run_job(_job("boom-job", workdir))
    assert success is False
    assert "boom" in (error or "")

    failed_task_id, failed_record = seen[0]
    assert failed_task_id.startswith("cron:boom-job:")
    assert failed_record == str(workdir)
    assert get_session_cwd(failed_task_id) is None

    # A second fire of the same job gets its own identity and a fresh record.
    _install_stubs(monkeypatch, _RecordingAgent)
    _RecordingAgent.sink = {}
    _RecordingAgent.barrier = None
    success, *_ = sched.run_job(_job("boom-job", workdir))
    assert success is True
    retry = next(iter(_RecordingAgent.sink.values()))
    assert retry["task_id"] != failed_task_id
    assert retry["first"]["record"] == str(workdir)
    assert get_session_cwd(retry["task_id"]) is None


def test_execution_id_makes_the_task_identity_traceable(
    recording_agent, monkeypatch, tmp_path
):
    """When the caller supplies the ledger's execution id, the task id uses it."""
    import cron.scheduler as sched

    workdir = tmp_path / "traced"
    workdir.mkdir()

    success, *_ = sched.run_job(
        _job("traced-job", workdir), execution_id="exec-abc123"
    )
    assert success is True
    entry = next(iter(recording_agent.sink.values()))
    assert entry["task_id"] == "cron:traced-job:exec-abc123"


def test_run_one_job_hands_its_execution_id_to_run_job(monkeypatch):
    """The ledger id reaches run_job, so a fire's cwd record is traceable."""
    import cron.scheduler as sched

    seen: list = []

    def fake_run_job(job, *, defer_agent_teardown=None, execution_id=None, **_kw):
        seen.append(execution_id)
        return True, "output", "response", None

    monkeypatch.setattr(sched, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(sched, "mark_execution_running", lambda *_a, **_kw: None)
    monkeypatch.setattr(sched, "finish_execution", lambda *_a, **_kw: None)
    monkeypatch.setattr(sched, "run_job", fake_run_job)
    monkeypatch.setattr(sched, "save_job_output", lambda *_a, **_kw: None)
    monkeypatch.setattr(sched, "_deliver_result", lambda *_a, **_kw: None)
    monkeypatch.setattr(sched, "mark_job_run", lambda *_a, **_kw: None)

    assert sched.run_one_job({"id": "ledger-job", "execution_id": "exec-42"}) is True
    assert seen == ["exec-42"]


# ---------------------------------------------------------------------------
# Pre-run scripts
# ---------------------------------------------------------------------------


def test_agent_prerun_script_runs_in_the_job_workdir(
    recording_agent, monkeypatch, tmp_path
):
    """The wake-gate script for an AGENT job runs in the configured workdir.

    A real subprocess: the script prints its own cwd, which is injected into
    the prompt, so the assertion is on where the interpreter actually ran.
    """
    import cron.scheduler as sched
    from hermes_constants import get_hermes_home

    workdir = tmp_path / "project"
    workdir.mkdir()
    scripts_dir = get_hermes_home() / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "where.py").write_text("import os\nprint(os.getcwd())\n")

    captured: dict = {}

    def capture_prompt(job, prerun_script=None, **_kw):
        captured["prerun"] = prerun_script
        return "hi"

    monkeypatch.setattr(sched, "_build_job_prompt", capture_prompt)

    success, *_ = sched.run_job(_job("script-job", workdir, script="where.py"))
    assert success is True

    ran_ok, stdout = captured["prerun"]
    assert ran_ok is True
    assert os.path.realpath(stdout.strip()) == os.path.realpath(str(workdir))


def test_no_agent_script_still_runs_in_the_job_workdir(monkeypatch, tmp_path):
    """The no_agent lane's existing workdir contract is preserved."""
    import cron.scheduler as sched
    from hermes_constants import get_hermes_home

    workdir = tmp_path / "watchdog"
    workdir.mkdir()
    scripts_dir = get_hermes_home() / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "where_no_agent.py").write_text("import os\nprint(os.getcwd())\n")

    success, _doc, final, _err = sched.run_job(
        _job("no-agent-job", workdir, no_agent=True, script="where_no_agent.py")
    )
    assert success is True
    assert os.path.realpath(final.strip()) == os.path.realpath(str(workdir))


# ---------------------------------------------------------------------------
# Dispatch lane: workdir jobs are ordinary parallel jobs now
# ---------------------------------------------------------------------------


def test_workdir_jobs_dispatch_on_the_parallel_pool_and_overlap(
    monkeypatch, tmp_path
):
    import cron.scheduler as sched

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    jobs = [
        {"id": "a", "name": "A", "workdir": str(dir_a)},
        {"id": "b", "name": "B", "workdir": str(dir_b)},
    ]

    sched._parallel_pool = None
    sched._parallel_pool_max_workers = None
    sched._running_job_ids.clear()

    monkeypatch.setattr(sched, "get_due_jobs", lambda: jobs)
    monkeypatch.setattr(sched, "claim_job_for_fire", lambda *_a, **_kw: True)

    barrier = threading.Barrier(2, timeout=10)
    calls: list = []
    calls_lock = threading.Lock()

    def fake_run_job(job, *, defer_agent_teardown=None, **_kw):
        with calls_lock:
            calls.append((job["id"], threading.current_thread().name))
        barrier.wait()
        return True, "output", "response", None

    monkeypatch.setattr(sched, "run_job", fake_run_job)
    monkeypatch.setattr(sched, "save_job_output", lambda *_a, **_kw: None)
    monkeypatch.setattr(sched, "mark_job_run", lambda *_a, **_kw: None)
    monkeypatch.setattr(sched, "_deliver_result", lambda *_a, **_kw: None)

    try:
        assert sched.tick(verbose=False, sync=True) == 2
    finally:
        sched._shutdown_parallel_pool()

    assert {job_id for job_id, _thread in calls} == {"a", "b"}
    assert all(thread.startswith("cron-parallel") for _job, thread in calls), calls


def test_a_resumed_workdir_job_dispatches_exactly_once_in_the_parallel_lane(
    monkeypatch, tmp_path
):
    """Fork-specific: the resume lane still dispatches one fire per loss."""
    import cron.scheduler as sched

    workdir = tmp_path / "resumed"
    workdir.mkdir()
    resume_job = {
        "id": "resume-me",
        "name": "resume-me",
        "workdir": str(workdir),
        "_resume_of": "exec-lost",
    }

    sched._parallel_pool = None
    sched._parallel_pool_max_workers = None
    sched._running_job_ids.clear()

    monkeypatch.setattr(sched, "_last_dead_owner_reap_at", None, raising=False)
    monkeypatch.setattr(sched, "get_due_jobs", lambda: [])
    monkeypatch.setattr(sched, "_collect_resume_jobs", lambda *_a, **_kw: [resume_job])
    monkeypatch.setattr(sched, "claim_job_for_fire", lambda *_a, **_kw: True)

    calls: list = []

    def fake_run_job(job, *, defer_agent_teardown=None, **_kw):
        calls.append((job["id"], threading.current_thread().name))
        return True, "output", "response", None

    monkeypatch.setattr(sched, "run_job", fake_run_job)
    monkeypatch.setattr(sched, "save_job_output", lambda *_a, **_kw: None)
    monkeypatch.setattr(sched, "mark_job_run", lambda *_a, **_kw: None)
    monkeypatch.setattr(sched, "_deliver_result", lambda *_a, **_kw: None)

    try:
        assert sched.tick(verbose=False, sync=True) == 1
    finally:
        sched._shutdown_parallel_pool()

    assert [job_id for job_id, _t in calls] == ["resume-me"]
    assert calls[0][1].startswith("cron-parallel"), calls
