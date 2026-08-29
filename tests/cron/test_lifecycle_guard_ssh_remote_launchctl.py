"""Regression tests: launchctl inside an ssh payload lands on the REMOTE host.

2026-08-28 denial audit: the launchctl guard blocked ``launchctl
submit/bootstrap``/gateway-label verbs anywhere in the command string,
including inside ``ssh acubens '...'`` payloads where the launchd job is
registered on the remote macOS host and cannot affect this host's gateway
supervisor (every audited firing was a false positive).

The exemption is launchctl-only: ssh-wrapped systemctl/hermes/pkill shapes
stay blocked (a self-ssh could still reach the local gateway), and every
LOCAL launchctl shape keeps failing closed.
"""

import pytest

from cron.lifecycle_guard import (
    contains_gateway_lifecycle_command,
    contains_gateway_lifecycle_command_or_referenced_script,
    contains_launchctl_submit_command,
)


class TestSshRemoteLaunchctlExempt:
    @pytest.mark.parametrize("command", [
        # Quoted remote payload, gateway-labeled kickstart on the remote host
        "ssh acubens 'launchctl kickstart -k gui/501/ai.hermes.gateway'",
        # Quoted remote submit with a hermes-gateway label
        'ssh acubens "launchctl submit -l ai.hermes.gateway-relay -- '
        '/usr/local/bin/helper"',
        # Unquoted remote command form
        "ssh -p 2222 acubens launchctl bootstrap gui/501 "
        "~/Library/LaunchAgents/ai.hermes.gateway.plist",
    ])
    def test_ssh_wrapped_launchctl_not_lifecycle(self, command):
        assert contains_gateway_lifecycle_command(command) is False
        assert contains_gateway_lifecycle_command_or_referenced_script(
            command
        ) is False

    def test_ssh_heredoc_launchctl_submit_not_blocked(self):
        command = (
            "ssh acubens <<'EOF'\n"
            "launchctl submit -l com.test.job -- /usr/bin/true\n"
            "EOF"
        )
        assert contains_launchctl_submit_command(command) is False
        assert contains_gateway_lifecycle_command_or_referenced_script(
            command
        ) is False

    def test_ssh_quoted_payload_submit_not_blocked(self):
        command = "ssh acubens 'launchctl submit -l com.test.job -- /usr/bin/true'"
        assert contains_launchctl_submit_command(command) is False


class TestLocalLaunchctlStillBlocked:
    @pytest.mark.parametrize("command", [
        "launchctl submit -l com.test.job -- /usr/bin/true",
        "launchctl bootstrap gui/501 ~/Library/LaunchAgents/some.plist",
        "cd /tmp && launchctl submit -l com.x -- /bin/sh run.sh",
    ])
    def test_local_submit_bootstrap_blocked(self, command):
        assert contains_launchctl_submit_command(command) is True

    def test_local_gateway_label_kickstart_blocked(self):
        assert contains_gateway_lifecycle_command(
            "launchctl kickstart -k gui/501/ai.hermes.gateway"
        ) is True

    def test_local_launchctl_after_ssh_command_still_blocked(self):
        # The exemption span ends at the top-level separator: the second
        # launchctl is LOCAL and must keep failing closed.
        command = (
            "ssh acubens 'launchctl submit -l remote.job -- /bin/true'; "
            "launchctl submit -l local.job -- /usr/bin/true"
        )
        assert contains_launchctl_submit_command(command) is True

    def test_local_launchctl_before_ssh_still_blocked(self):
        command = (
            "launchctl submit -l local.job -- /usr/bin/true && ssh acubens uptime"
        )
        assert contains_launchctl_submit_command(command) is True


class TestSshExemptionIsLaunchctlOnly:
    @pytest.mark.parametrize("command", [
        # A self-ssh could still reach the local gateway; only launchctl
        # (which requires macOS launchd on the ssh TARGET) is exempted.
        "ssh sol 'systemctl restart hermes-gateway'",
        "ssh sol 'hermes gateway restart'",
    ])
    def test_ssh_wrapped_non_launchctl_shapes_stay_blocked(self, command):
        assert contains_gateway_lifecycle_command_or_referenced_script(
            command
        ) is True
