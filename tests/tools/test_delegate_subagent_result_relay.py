"""A finished subagent's whole reply reaches progress consumers as ``result``.

``summary`` on the ``subagent.complete`` event stays a 500-character preview for
the consumers that show a line (gateway watch window, TUI mirror, live log);
``result`` carries the reply itself, up to SUBAGENT_RESULT_MAX_CHARS, for the
ACP adapter, whose clients let the user expand it.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


class _FinishingChild:
    """Minimal stand-in for an AIAgent subagent that finishes with a reply."""

    def __init__(self, reply: str):
        self._reply = reply
        self._subagent_id = "sa-0-relay01"
        self._delegate_depth = 1
        self._delegate_role = "leaf"
        self.model = "test/model"
        self.provider = "testprov"
        self.api_mode = "chat_completions"
        self.base_url = "https://example.test/v1"
        self.max_iterations = 30
        self.quiet_mode = True
        self.skip_memory = True
        self.skip_context_files = True
        self.platform = "cli"
        self.ephemeral_system_prompt = "sys prompt"
        self.enabled_toolsets = ["terminal"]
        self.valid_tool_names = {"terminal"}
        self.tools = [{"name": "terminal", "description": "shell"}]
        self.session_prompt_tokens = 10
        self.session_completion_tokens = 20
        self.events: list[tuple[str, dict]] = []
        self.tool_progress_callback = self._record

    def _record(self, event_type, *args, **kwargs):
        self.events.append((event_type, kwargs))

    def get_activity_summary(self):
        return {"api_call_count": 1, "max_iterations": 30, "current_tool": None,
                "seconds_since_activity": 0}

    def run_conversation(self, *args, **kwargs):
        return {"final_response": self._reply, "completed": True, "messages": [],
                "api_calls": 1}

    def interrupt(self, *args, **kwargs):
        pass

    def close(self):
        pass


def test_complete_event_carries_the_whole_reply(hermes_home, monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 30.0)
    reply = "Line one of the report.\n" + "x" * 3000 + "\nThe last line."
    child = _FinishingChild(reply)
    parent = MagicMock()
    parent._touch_activity = MagicMock()
    parent._current_task_id = None

    result = delegate_tool._run_single_child(
        task_index=0, goal="write a report", child=child, parent_agent=parent,
    )

    assert result["status"] == "completed"
    complete = [kw for event, kw in child.events if event == "subagent.complete"]
    assert len(complete) == 1
    assert complete[0]["result"] == reply
    assert complete[0]["summary"] == reply[:500]


def test_result_is_capped(hermes_home, monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 30.0)
    child = _FinishingChild("y" * (delegate_tool.SUBAGENT_RESULT_MAX_CHARS + 50))
    parent = MagicMock()
    parent._touch_activity = MagicMock()
    parent._current_task_id = None

    delegate_tool._run_single_child(task_index=0, goal="g", child=child, parent_agent=parent)

    complete = [kw for event, kw in child.events if event == "subagent.complete"]
    assert len(complete[0]["result"]) == delegate_tool.SUBAGENT_RESULT_MAX_CHARS
