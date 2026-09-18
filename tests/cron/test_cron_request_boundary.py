"""Cron request boundary: recall intent is separated from script evidence, and
an oversized inline request fails the job explicitly before memory prefetch or
any model/fallback call.

Regression for the 2026-09-17/18 Nightly Lantern incidents: the scheduler
prepended multi-megabyte script output to the user instruction, the whole
packet became the holographic recall query, and the model then rejected the
request across the fallback chain. The job's own prompt (plus per-run
context) is now the memory query; the assembled packet is checked against
the model's context window and, when it cannot fit, retained as an artifact
and reported as an explicit per-job failure without running the agent.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cron.scheduler import (
    _check_cron_request_size,
    _job_memory_query,
    run_job,
)

_RUNTIME = {
    "api_key": "test-key",
    "base_url": "https://example.invalid/v1",
    "provider": "openrouter",
    "api_mode": "chat_completions",
}


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "cron").mkdir()
    (hermes_home / "cron" / "output").mkdir()
    (hermes_home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    import cron.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")
    return hermes_home


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_job_memory_query_defaults_to_the_job_prompt_not_script_output():
    job = {"prompt": "Reflect on the day.", "script": "collect.py"}
    assert _job_memory_query(job) == "Reflect on the day."


def test_job_memory_query_includes_per_run_context():
    job = {"prompt": "Reflect on the day."}
    query = _job_memory_query(job, extra_prompt="focus on the deploy")
    assert "Reflect on the day." in query
    assert "focus on the deploy" in query


def test_job_memory_query_honours_explicit_field_and_skip():
    assert _job_memory_query({"prompt": "long prompt", "memory_query": "deploy status"}) == "deploy status"
    assert _job_memory_query({"prompt": "self-contained packet", "memory_query": ""}) == ""


def test_check_cron_request_size_fits_and_overflows():
    assert _check_cron_request_size("short prompt", context_length=8000) is None
    huge = "evidence " * 200_000
    verdict = _check_cron_request_size(huge, context_length=8000)
    assert verdict is not None
    assert verdict["estimated_tokens"] > 8000
    assert verdict["context_length"] == 8000
    assert verdict["prompt_chars"] == len(huge)


# ---------------------------------------------------------------------------
# End-to-end through run_job with real script execution
# ---------------------------------------------------------------------------


def _run(job, context_length):
    mock_agent = MagicMock()
    mock_agent.context_compressor.context_length = context_length
    mock_agent.run_conversation.return_value = {"final_response": "ok", "completed": True}
    with patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state.SessionDB"), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=_RUNTIME), \
         patch("run_agent.AIAgent") as mock_agent_cls:
        mock_agent_cls.return_value = mock_agent
        result = run_job(job)
    return result, mock_agent, mock_agent_cls


def test_run_job_passes_recall_intent_separate_from_script_evidence(cron_env):
    script = cron_env / "scripts" / "collect.py"
    script.write_text('print("EVIDENCE " * 2000)\n')
    job = {"id": "boundary-ok", "name": "boundary", "prompt": "Report any notable changes.", "script": str(script)}

    (success, output, final_response, error), mock_agent, _ = _run(job, context_length=200_000)

    assert success is True, error
    assert final_response == "ok"
    mock_agent.run_conversation.assert_called_once()
    args, kwargs = mock_agent.run_conversation.call_args
    assert "## Script Output" in args[0]
    assert "EVIDENCE" in args[0]
    assert kwargs["memory_query"] == "Report any notable changes."


def test_run_job_refuses_oversized_inline_request_before_memory_or_model(cron_env):
    script = cron_env / "scripts" / "collect.py"
    script.write_text('print("EVIDENCE " * 100000)\n')
    job = {"id": "boundary-big", "name": "boundary", "prompt": "Reflect on the day.", "script": str(script)}

    (success, output, final_response, error), mock_agent, _ = _run(job, context_length=8000)

    assert success is False
    assert final_response == ""
    mock_agent.run_conversation.assert_not_called()
    assert error and "context" in error.lower()
    # Compact failure doc — never the multi-megabyte prompt echoed back.
    assert len(output) < 20_000
    assert "context" in output.lower()

    retained = list((cron_env / "cron" / "oversized").rglob("*"))
    files = [p for p in retained if p.is_file()]
    assert files, "oversized request was not retained as an artifact"
    assert files[0].stat().st_size > 500_000
    assert str(files[0]) in output
    # Not dropped into the job output dir where context_from would ingest it.
    assert not list((cron_env / "cron" / "output").rglob("*.md"))


def test_run_job_without_context_length_still_runs(cron_env):
    """A runtime with no resolvable window (or a test double) must not be
    blocked by the size check."""
    job = {"id": "boundary-nolen", "name": "boundary", "prompt": "Say hi."}
    (success, _output, final_response, error), mock_agent, _ = _run(job, context_length=MagicMock())
    assert success is True, error
    mock_agent.run_conversation.assert_called_once()
