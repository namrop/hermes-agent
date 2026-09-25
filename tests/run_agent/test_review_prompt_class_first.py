"""Behavior tests for the skill review / combined review prompts.

Keeper ruling 2026-09-24 (task 633): the post-turn review no longer grows the
skill library. "Nothing to save." is a normal result; its one direct skill
write is patching a skill loaded in the conversation that turned out wrong;
every other pattern is recorded with beam_photon in the owning Atrium beam's
photon ledger, after searching memory photons for corroboration.

User-preference corrections (style, format, verbosity, legibility) are
first-class skill signals, not just memory signals.

These tests assert behavioral *instructions* are present — they do NOT
snapshot the full prompt text (change-detector).
"""

from run_agent import AIAgent


# ---------------------------------------------------------------------------
# _SKILL_REVIEW_PROMPT
# ---------------------------------------------------------------------------

def _assert_beam_photon_routing(prompt: str, label: str) -> None:
    lower = prompt.lower()
    # No write quota: inaction is a normal outcome.
    assert "be active" not in lower, f"{label}: write quota must be gone"
    assert "missed learning opportunity" not in lower, f"{label}: inaction is not a miss"
    assert "'nothing to save.' is a normal result" in lower, f"{label}: nothing-to-save must be normal"
    # The one direct write, and what is refused.
    assert "only direct skill write" in lower, f"{label}: must name the one direct write"
    assert "loaded in this conversation" in lower
    assert "action=patch" in prompt
    assert "refused" in lower
    # Everything else goes to the beam photon ledger, memory searched first.
    assert "beam_photon" in prompt
    assert "action=search_memory" in prompt and "action=record" in prompt
    assert "memory_queries" in prompt and "memory_fact_ids" in prompt
    assert "not memory photons" in lower, f"{label}: must keep the two photon senses apart"
    # Old accretion paths are gone.
    assert "write_file" not in prompt, f"{label}: support-file writes are no longer offered"
    assert "CREATE A NEW CLASS-LEVEL UMBRELLA" not in prompt


def test_skill_review_prompt_routes_patterns_to_beam_photons():
    _assert_beam_photon_routing(AIAgent._SKILL_REVIEW_PROMPT, "_SKILL_REVIEW_PROMPT")


def test_combined_review_prompt_routes_patterns_to_beam_photons():
    _assert_beam_photon_routing(AIAgent._COMBINED_REVIEW_PROMPT, "_COMBINED_REVIEW_PROMPT")


def test_skill_review_prompt_treats_user_corrections_as_skill_signal():
    """Style/format/verbosity complaints must be FIRST-CLASS skill signals, not just memory."""
    prompt = AIAgent._SKILL_REVIEW_PROMPT
    lower = prompt.lower()
    # Must mention style/format/verbosity-family corrections
    assert any(k in lower for k in ("style", "format", "verbos", "legib", "tone")), (
        "must name style/format/verbosity/legibility as signals"
    )
    # Must frame these as first-class skill signals (not memory-only)
    assert "FIRST-CLASS" in prompt or "first-class" in prompt, (
        "must explicitly label user-preference corrections as first-class skill signals"
    )
    # Must mention the correction-type phrases to tune the model's ear
    assert "stop doing" in lower or "don't" in lower or "hate" in lower or "frustrat" in lower, (
        "must give concrete phrasing examples so the model recognizes corrections"
    )
















# ---------------------------------------------------------------------------
# _COMBINED_REVIEW_PROMPT
# ---------------------------------------------------------------------------

def test_combined_review_prompt_has_memory_section():
    """Memory half must still cover user facts and preferences."""
    prompt = AIAgent._COMBINED_REVIEW_PROMPT
    assert "**Memory**" in prompt
    # Keeper ruling 2026-09-25: memory photons go to fact_store, not the trunk.
    assert "fact_store action=add" in prompt














# ---------------------------------------------------------------------------
# Anti-pattern guidance — see issue #6051. The reviewer was learning transient
# environment failures (e.g. "browser tools do not work" from a fresh-install
# Playwright miss) as durable skill rules, then citing them against itself for
# weeks after the environment was fixed. Both review prompts must explicitly
# tell the reviewer not to capture environment-dependent or negative-framing
# content as skills.
# ---------------------------------------------------------------------------


def _assert_anti_pattern_guidance(prompt: str, label: str) -> None:
    """Both review prompts must carry the same anti-pattern section."""
    lower = prompt.lower()
    assert "do not capture" in lower, (
        f"{label}: must have an explicit 'Do NOT capture' section"
    )
    # Environment-dependent failures (the #6051 root cause)
    assert any(k in lower for k in ("missing binar", "command not found", "uninstalled", "fresh-install")), (
        f"{label}: must call out environment/setup failures as not-skill-worthy"
    )
    # Negative-framing avoidance
    assert any(k in lower for k in ("negative claim", "do not work", "is broken")), (
        f"{label}: must call out negative-claim phrasings as the failure mode"
    )
    # Positive reframing — "capture the fix, not the failure"
    assert "capture the fix" in lower or "capture the fix " in lower, (
        f"{label}: must redirect tool-failure capture toward the fix, not the constraint"
    )
    # One-off task narratives (#12812 family)
    assert "one-off" in lower, (
        f"{label}: must call out one-off task narratives as not-skill-worthy"
    )


def _assert_unresolved_failure_guidance(prompt: str, label: str) -> None:
    """Unresolved task attempts must not become persistent skill guidance."""
    lower = prompt.lower()
    assert "unresolved failures" in lower, f"{label}: must identify unresolved failures"
    assert "working method" in lower, f"{label}: must require a working method"
    assert "told the user to check manually" in lower, (
        f"{label}: must recognize an explicitly unresolved session"
    )
    assert "never the dead ends" in lower, f"{label}: must exclude failed attempts"
    assert "independently confident" in lower, (
        f"{label}: must limit exceptions to verified alternatives"
    )


def test_skill_review_prompt_rejects_unresolved_failures():
    _assert_unresolved_failure_guidance(AIAgent._SKILL_REVIEW_PROMPT, "_SKILL_REVIEW_PROMPT")


def test_combined_review_prompt_rejects_unresolved_failures():
    _assert_unresolved_failure_guidance(AIAgent._COMBINED_REVIEW_PROMPT, "_COMBINED_REVIEW_PROMPT")






# ---------------------------------------------------------------------------
# _MEMORY_REVIEW_PROMPT — unchanged, still memory-focused
# ---------------------------------------------------------------------------
