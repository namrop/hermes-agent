"""Real checkpoint failures propagate honestly through CLI and gateway rollback."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools.checkpoint_manager import CheckpointManager


@pytest.fixture
def rollback_project(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", tmp_path / "checkpoints")
    project = tmp_path / "project"
    project.mkdir()
    target = project / "main.py"
    target.write_text("original\n")
    manager = CheckpointManager(enabled=True)
    assert manager.ensure_checkpoint(str(project))
    target.write_text("user edit without ledger\n")
    monkeypatch.setenv("TERMINAL_CWD", str(project))
    return manager, target


@pytest.mark.parametrize("flag", ["", "--all", "--force"])
def test_cli_reports_missing_ledger_without_undoing_chat(rollback_project, capsys, flag):
    from hermes_cli.cli_commands_mixin import CLICommandsMixin

    manager, target = rollback_project
    cli = SimpleNamespace(
        agent=SimpleNamespace(_checkpoint_mgr=manager),
        _resolve_checkpoint_ref=lambda ref, checkpoints: checkpoints[0]["hash"],
        conversation_history=[{"role": "user", "content": "keep my turn"}],
        undo_last=Mock(),
    )
    CLICommandsMixin._handle_rollback_command(cli, f"/rollback 1 {flag}")
    output = capsys.readouterr().out
    if flag:
        assert target.read_text() == "original\n"
        assert "pre-rollback snapshot was saved" in output
        cli.undo_last.assert_called_once_with(prefill=False)
    else:
        assert target.read_text() == "user edit without ledger\n"
        assert "selective rollback cannot be proven" in output.lower()
        assert "--all" in output and "--force" in output
        assert "pre-rollback snapshot was saved" not in output
        assert "Restored to checkpoint" not in output
        cli.undo_last.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["", "--all", "--force"])
async def test_gateway_reports_missing_ledger_and_explicit_full_restore(
    rollback_project, monkeypatch, flag,
):
    from gateway.slash_commands import GatewaySlashCommandsMixin

    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {"checkpoints": {"enabled": True}})
    _, target = rollback_project
    event = SimpleNamespace(get_command_args=lambda: f"1 {flag}")
    output = await GatewaySlashCommandsMixin._handle_rollback_command(SimpleNamespace(), event)
    if flag:
        assert target.read_text() == "original\n"
        assert "restored" in output.lower()
    else:
        assert target.read_text() == "user edit without ledger\n"
        assert "selective rollback cannot be proven" in output.lower()
        assert "--all" in output and "--force" in output
        assert "pre-rollback snapshot" not in output.lower()
