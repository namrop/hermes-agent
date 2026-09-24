"""Post-turn review scope: one direct skill write (keeper ruling 2026-09-24, task 633)."""

import contextvars
import json

from agent.background_review import skills_loaded_in_conversation, summarize_background_review_actions
from tools import skill_manager_tool as smt
from tools.skill_provenance import reset_current_write_origin, set_current_write_origin


def _in_review(loaded, fn):
    def run():
        token = set_current_write_origin("background_review")
        try:
            if loaded is not None:
                smt.begin_post_turn_review_scope(loaded)
            return fn()
        finally:
            reset_current_write_origin(token)
    return contextvars.copy_context().run(run)


def test_patch_of_loaded_skill_passes_guard():
    assert _in_review({"oasis-mailroom-operations"}, lambda: smt._post_turn_review_guard("patch", "oasis-mailroom-operations")) is None
    assert _in_review({"productivity/oasis-mailroom-operations"}, lambda: smt._post_turn_review_guard("edit", "oasis-mailroom-operations")) is None


def test_other_writes_are_refused_with_beam_photon_pointer():
    for action, name in (("patch", "not-loaded"), ("create", "x"), ("write_file", "oasis-mailroom-operations"), ("delete", "oasis-mailroom-operations"), ("remove_file", "oasis-mailroom-operations")):
        refusal = _in_review({"oasis-mailroom-operations"}, lambda: smt._post_turn_review_guard(action, name))
        assert refusal and refusal["success"] is False, (action, name)
        assert "beam_photon" in refusal["error"]


def test_skill_manage_refuses_create_before_touching_disk():
    result = json.loads(_in_review(set(), lambda: smt.skill_manage(action="create", name="new-umbrella", content="---\nname: x\n---\n")))
    assert result["success"] is False and "does not create" in result["error"]


def test_no_scope_means_no_restriction():
    # Curator fork: background origin, no post-turn scope installed.
    assert _in_review(None, lambda: smt._post_turn_review_guard("delete", "anything")) is None
    # Foreground: scope ContextVar may be set, origin is not background review.
    def foreground():
        smt.begin_post_turn_review_scope(set())
        return smt._post_turn_review_guard("create", "anything")
    assert contextvars.copy_context().run(foreground) is None


def test_skills_loaded_in_conversation():
    messages = [
        {"role": "user", "content": '[IMPORTANT: The user has invoked the "vikunja" skill, indicating they want you to follow its instructions. The full skill content is loaded below.]'},
        {"role": "assistant", "tool_calls": [
            {"id": "1", "function": {"name": "skill_view", "arguments": json.dumps({"name": "atrium-navigation"})}},
            {"id": "2", "function": {"name": "skill_manage", "arguments": json.dumps({"name": "ignored"})}},
            {"id": "3", "function": {"name": "skill_view", "arguments": "not json"}},
        ]},
        {"role": "user", "content": [{"type": "text", "text": "plain turn"}]},
    ]
    assert skills_loaded_in_conversation(messages) == {"vikunja", "atrium-navigation"}


def test_summary_surfaces_recorded_beam_photon_only():
    review = [
        {"role": "assistant", "tool_calls": [
            {"id": "a", "function": {"name": "beam_photon", "arguments": json.dumps({"action": "search_memory", "query": "q"})}},
            {"id": "b", "function": {"name": "beam_photon", "arguments": json.dumps({"action": "record", "title": "t"})}},
        ]},
        {"role": "tool", "tool_call_id": "a", "content": json.dumps({"success": True, "matches": []})},
        {"role": "tool", "tool_call_id": "b", "content": json.dumps({"success": True, "message": "Beam photon P-2026-09-24-001 recorded in x.jsonl"})},
    ]
    actions = summarize_background_review_actions(review, [])
    assert actions == ["🔆 Beam photon P-2026-09-24-001 recorded in x.jsonl"]
