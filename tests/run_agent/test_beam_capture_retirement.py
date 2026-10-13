"""Luis retired automatic beam-ledger capture on 2026-10-11."""
import contextvars

from agent.background_review import (
    _COMBINED_REVIEW_PROMPT,
    _SKILL_REVIEW_PROMPT,
    review_tool_names,
)
from tools import skill_manager_tool
from tools.skill_provenance import reset_current_write_origin, set_current_write_origin


def test_review_prompts_do_not_instruct_beam_photon_writes():
    for prompt in (_SKILL_REVIEW_PROMPT, _COMBINED_REVIEW_PROMPT):
        assert "beam_photon action=record" not in prompt
        assert "beam_photon action=route" not in prompt
        assert "leave other patterns unrecorded" in prompt.lower()
        assert "action=patch" in prompt


def test_review_cannot_dispatch_beam_photon_tool():
    names = review_tool_names({"beam_photon", "skill_manage", "fact_store", "skill_view"})
    assert "beam_photon" not in names
    assert "fact_store" in names
    assert "skill_manage" in names


def test_refused_review_write_does_not_redirect_to_beam_ledger():
    def run():
        token = set_current_write_origin("background_review")
        try:
            skill_manager_tool.begin_post_turn_review_scope(set())
            result = skill_manager_tool._post_turn_review_guard("create", "new-skill")
            assert result is not None
            assert result["success"] is False
            assert "beam_photon" not in result["error"]
            assert "unrecorded" in result["error"]
        finally:
            reset_current_write_origin(token)
    contextvars.copy_context().run(run)
