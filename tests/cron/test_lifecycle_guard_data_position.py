"""Regression tests: data-position files must not trip the lifecycle guard.

2026-08-28 denial audit: 22/22 firings of the gateway self-restart guard
were false positives — none of the blocked commands touched the gateway.
Three mechanisms, all covered here:

(a) Path tokens inside python payloads (heredocs, ``python -c`` bodies with
    newlines) land in "command position" of a synthetic shlex segment after
    a ``(`` punctuation split, get resolved, and their CONTENTS scanned.
    A read-only look at a file whose help text quotes
    ``hermes gateway restart`` (hermes_cli/web_server.py) was blocked, as
    were data files (cron/jobs.json, session exports) that merely quote
    lifecycle commands.
(b) The ``p?kill ... hermes ... gateway`` regex branch spans unbounded
    prose on single-line files — a one-line JSON matched across kilobytes.
(c) Any referenced file >1MB was blocked regardless of content,
    deterministically, with an error claiming a restart attempt.

True positives that must KEEP failing closed are asserted at the bottom.
"""

import json

import pytest

from cron.lifecycle_guard import (
    _MAX_REFERENCED_SCRIPT_BYTES,
    contains_gateway_lifecycle_command,
    contains_gateway_lifecycle_command_or_referenced_script,
    gateway_lifecycle_block_reason,
)


@pytest.fixture()
def docstring_file(tmp_path):
    """A file whose help text quotes a lifecycle command (web_server.py shape)."""
    path = tmp_path / "web_server.py"
    path.write_text(
        '"""Web server CLI.\n\n'
        "If the gateway is stuck, run `hermes gateway restart` from a\n"
        'separate shell.\n"""\n'
        'USAGE = "hermes gateway restart"\n'
    )
    return path


class TestDataPositionReadsAreNotBlocked:
    """Mechanism (a): read operands are data, not referenced scripts."""

    def test_python_heredoc_open_of_docstring_file(self, docstring_file, tmp_path):
        command = (
            "python3 - <<'EOF'\n"
            f"text = open('{docstring_file}').read()\n"
            "print(text)\n"
            "EOF"
        )
        assert contains_gateway_lifecycle_command_or_referenced_script(
            command, cwd=str(tmp_path)
        ) is False

    def test_python_c_with_newlines_open_of_data_file(self, tmp_path):
        jobs = tmp_path / "jobs.json"
        jobs.write_text(json.dumps(
            {"jobs": [{"prompt": "when stuck run hermes gateway restart"}]}
        ))
        command = (
            'python3 -c "import json\n'
            f"data = json.load(open('{jobs}'))\n"
            'print(len(data))"'
        )
        assert contains_gateway_lifecycle_command_or_referenced_script(
            command, cwd=str(tmp_path)
        ) is False

    def test_oversized_data_position_file_is_not_blocked(self, tmp_path):
        """Mechanism (c): >1MB files in data position are skipped, not
        fail-closed — the old behavior blocked deterministically (a retry
        could never work) with an error claiming a restart attempt."""
        big = tmp_path / "big.json"
        big.write_text('{"data":"' + "a" * (_MAX_REFERENCED_SCRIPT_BYTES + 100) + '"}')
        command = (
            "python3 - <<'EOF'\n"
            f"d = open('{big}').read()\n"
            "EOF"
        )
        assert contains_gateway_lifecycle_command_or_referenced_script(
            command, cwd=str(tmp_path)
        ) is False


class TestKillRegexWindowBounded:
    """Mechanism (b): the kill branch must not span kilobytes of prose."""

    def test_one_line_json_with_distant_tokens_does_not_match(self):
        text = json.dumps({
            "note": "watchdog killed a stale session",
            "filler": "x" * 2000,
            "service": "hermes",
            "component": "gateway",
        })
        assert "\n" not in text
        assert contains_gateway_lifecycle_command(text) is False

    def test_real_pkill_of_gateway_still_matches(self):
        assert contains_gateway_lifecycle_command(
            "pkill -f 'hermes gateway'"
        ) is True
        assert contains_gateway_lifecycle_command(
            "kill $(pgrep -f 'gateway run --profile hermes')"
        ) is True


class TestBlockReasonNamesTheMatch:
    """The block must name the file/token that matched, so the agent can
    adapt instead of blind-retrying."""

    def test_direct_command_reason_names_matched_text(self):
        reason = gateway_lifecycle_block_reason("hermes gateway restart")
        assert reason is not None
        assert "hermes gateway restart" in reason

    def test_referenced_script_reason_names_the_file(self, tmp_path):
        script = tmp_path / "evil.sh"
        script.write_text("#!/bin/sh\nsystemctl restart hermes-gateway\n")
        reason = gateway_lifecycle_block_reason(
            f"bash {script}", cwd=str(tmp_path)
        )
        assert reason is not None
        assert script.name in reason

    def test_benign_command_has_no_reason(self):
        assert gateway_lifecycle_block_reason("echo hello") is None


class TestExecutionPositionStillFailsClosed:
    """True positives: anything that would actually execute stays blocked."""

    def test_systemctl_restart_blocked(self):
        assert contains_gateway_lifecycle_command_or_referenced_script(
            "systemctl restart hermes-gateway"
        ) is True

    def test_hermes_gateway_restart_blocked(self):
        assert contains_gateway_lifecycle_command_or_referenced_script(
            "hermes gateway restart"
        ) is True

    def test_executed_script_with_lifecycle_contents_blocked(self, tmp_path):
        script = tmp_path / "restart.sh"
        script.write_text("#!/bin/sh\nhermes gateway restart\n")
        for command in (f"bash {script}", f"{script}", f"source {script}"):
            assert contains_gateway_lifecycle_command_or_referenced_script(
                command, cwd=str(tmp_path)
            ) is True, f"must stay blocked: {command}"

    def test_oversized_executed_script_still_fails_closed(self, tmp_path):
        big = tmp_path / "big.sh"
        big.write_text("#!/bin/sh\n" + ("# pad\n" * 300000))
        assert big.stat().st_size > _MAX_REFERENCED_SCRIPT_BYTES
        assert contains_gateway_lifecycle_command_or_referenced_script(
            f"bash {big}", cwd=str(tmp_path)
        ) is True

    def test_shell_c_wrapping_script_still_scanned(self, tmp_path):
        script = tmp_path / "wrapped.sh"
        script.write_text("systemctl restart hermes-gateway\n")
        assert contains_gateway_lifecycle_command_or_referenced_script(
            f'sh -c "bash {script}"', cwd=str(tmp_path)
        ) is True
