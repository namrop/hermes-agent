"""Cwd consumers that must survive removal of cron's global env override."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def scoped_workspace(tmp_path, monkeypatch):
    from agent.runtime_cwd import _SESSION_CWD, set_session_cwd
    from tools.terminal_tool import clear_session_cwd, record_session_cwd

    ambient = tmp_path / "ambient"
    project = tmp_path / "project"
    current = project / "nested"
    explicit = tmp_path / "explicit"
    for directory in (ambient, project, current, explicit):
        directory.mkdir(parents=True, exist_ok=True)
    for directory in (ambient, project):
        (directory / ".git").mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(ambient))
    token = set_session_cwd(str(project))
    task_id = "cron:scoped-consumers:execution"
    record_session_cwd(task_id, str(current))
    try:
        yield SimpleNamespace(
            ambient=ambient, project=project, current=current,
            explicit=explicit, task_id=task_id,
        )
    finally:
        clear_session_cwd(task_id)
        _SESSION_CWD.reset(token)


def test_project_skill_root_uses_session_directory(scoped_workspace):
    from agent.skill_utils import find_project_root

    assert find_project_root() == scoped_workspace.project
    assert find_project_root(start=scoped_workspace.ambient) == scoped_workspace.ambient


@pytest.mark.parametrize("recorded", [True, False])
def test_delegate_workspace_hint_uses_parent_task_then_session(scoped_workspace, recorded):
    from tools.delegate_tool import _resolve_workspace_hint

    parent = SimpleNamespace(
        _current_task_id=scoped_workspace.task_id if recorded else None,
        _subdirectory_hints=SimpleNamespace(working_dir=scoped_workspace.project),
    )
    expected = scoped_workspace.current if recorded else scoped_workspace.project
    assert _resolve_workspace_hint(parent) == str(expected)


@pytest.mark.parametrize("explicit", [True, False])
def test_terminal_checkpoint_follows_the_command_task_cwd(scoped_workspace, explicit):
    from agent.tool_executor import _begin_tool_execution

    checkpoint = SimpleNamespace(enabled=True, ensure_checkpoint=Mock())
    agent = SimpleNamespace(
        quiet_mode=True,
        _touch_activity=Mock(),
        tool_progress_callback=None,
        tool_start_callback=None,
        _checkpoint_mgr=checkpoint,
    )
    arguments = {"command": "rm -rf ./obsolete"}
    if explicit:
        arguments["workdir"] = str(scoped_workspace.explicit)
    _begin_tool_execution(
        agent,
        function_name="terminal",
        function_args=arguments,
        effective_task_id=scoped_workspace.task_id,
        tool_call_id="checkpoint-test",
        display_index=None,
    )
    expected = scoped_workspace.explicit if explicit else scoped_workspace.current
    checkpoint.ensure_checkpoint.assert_called_once()
    assert Path(checkpoint.ensure_checkpoint.call_args.args[0]) == expected


def test_agent_initializes_subdirectory_hints_in_session_workspace(scoped_workspace):
    from run_agent import AIAgent

    agent = AIAgent(
        provider="openai", model="test-model", api_key="test-key",
        base_url="https://example.invalid/v1", api_mode="chat_completions",
        enabled_toolsets=[], quiet_mode=True,
        skip_context_files=True, skip_memory=True,
    )
    try:
        assert agent._subdirectory_hints.working_dir == scoped_workspace.project
    finally:
        close = getattr(agent, "close", None)
        if callable(close):
            close()
