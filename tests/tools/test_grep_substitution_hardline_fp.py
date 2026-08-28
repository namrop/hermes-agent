"""Regression tests: ambiguous grep parses must not hardline-block (audit 2026-08-28).

Every observed firing (14/14) of the malformed-grep -> unconditional hardline
escalation in the 2026-08-28 denial audit was a false positive: short,
heredoc-free, read-only diagnostics whose only sin was a grep inside a
double-quoted "$(...)" command substitution. The fail-closed contract stated
on ``_quoted_grep_pattern_spans`` is "mask nothing and scan the ORIGINAL
command" on an uncertain parse — not "block unconditionally".

Covers:
- the audited real-world one-liners parse cleanly and are not blocked
- a genuinely ambiguous parse falls back to scanning the unmasked text,
  which still catches real hardline content riding along
- the hardline floor itself is untouched
- the residual malformed-payload block message names the actual trigger
  (unresolvable quoting/substitution), not "heredocs, giant one-liners"
"""

import pytest

from tools.approval import (
    _MALFORMED_EXEC_DESCRIPTION,
    _PARSER_LIMIT_DESCRIPTION,
    _hardline_block_result,
    _quoted_grep_pattern_spans,
    detect_dangerous_command,
    detect_hardline_command,
)


# The audited real-world shapes (verbatim mechanism, sanitized operands).
_AUDITED_FALSE_POSITIVES = [
    # grep inside a double-quoted command substitution feeding sed a range
    """sed -n "$(grep -n 'x' f | cut -d: -f1),+18p" f""",
    # grep inside a substitution building a curl header
    'curl -H "Authorization: Bearer $(grep KEY env | cut -d= -f2)" '
    "https://api.example.com/v1/status",
]


@pytest.mark.parametrize("command", _AUDITED_FALSE_POSITIVES)
def test_audited_grep_substitution_oneliners_are_not_hardline(command):
    assert detect_hardline_command(command) == (False, None)


@pytest.mark.parametrize("command", _AUDITED_FALSE_POSITIVES)
def test_audited_grep_substitution_oneliners_are_not_dangerous(command):
    dangerous, _, description = detect_dangerous_command(command)
    assert dangerous is False, f"unexpected dangerous match: {description}"


@pytest.mark.parametrize("command", _AUDITED_FALSE_POSITIVES)
def test_grep_inside_quoted_substitution_parses_unambiguously(command):
    _, malformed = _quoted_grep_pattern_spans(command)
    assert malformed is False


def test_truly_ambiguous_parse_scans_original_instead_of_blocking():
    # An unterminated quote keeps the parse ambiguous — a benign command
    # must pass (scanned unmasked, nothing matches)...
    command = 'grep -P "unclosed pattern f'
    _, malformed = _quoted_grep_pattern_spans(command)
    assert malformed is True
    assert detect_hardline_command(command) == (False, None)


def test_ambiguous_parse_fallback_still_catches_hardline_content():
    # ...while hardline content riding beside the ambiguous grep is still
    # caught, because the fallback scans the ORIGINAL unmasked text.
    # (grep -e with no pattern operand is an ambiguous parse.)
    command = "grep -e; rm -rf /"
    _, malformed = _quoted_grep_pattern_spans(command)
    assert malformed is True
    is_hardline, description = detect_hardline_command(command)
    assert is_hardline is True
    assert description == "recursive delete of root filesystem"


def test_dangerous_command_beside_grep_substitution_still_blocked():
    command = """sed -n "$(grep -n 'x' f),p" f && rm -rf /"""
    is_hardline, description = detect_hardline_command(command)
    assert is_hardline is True
    assert description == "recursive delete of root filesystem"


def test_masked_pcre_pattern_exemption_survives_tokenizer_change():
    # The whole point of the grep-span masking: hardline text used as a
    # quoted grep -P pattern is data, not a command.
    command = "grep -P 'rm -rf --no-preserve-root /' audit.log"
    assert detect_hardline_command(command) == (False, None)


def test_hardline_floor_unchanged():
    assert detect_hardline_command("rm -rf /")[0] is True
    assert detect_hardline_command(":(){ :|:& };:")[0] is True


def test_malformed_recovery_text_names_quoting_not_heredocs():
    result = _hardline_block_result(_MALFORMED_EXEC_DESCRIPTION)
    assert "RECOVERY" in result["message"]
    # The 2026-08-28 audit: blaming "heredocs, giant one-liners" was wrong
    # in 14/14 firings and prevented agent adaptation.
    assert "heredocs" not in result["message"]
    assert "quoting" in result["message"] or "substitution" in result["message"]


def test_parser_limit_recovery_text_still_names_payload_size():
    result = _hardline_block_result(_PARSER_LIMIT_DESCRIPTION)
    assert "heredocs" in result["message"]
