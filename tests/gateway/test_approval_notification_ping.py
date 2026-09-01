"""Behavior contract for exec-approval requester pings.

Restores the approval half of primary ``995a564b94`` ("fix: restore
Discord approval notification pings"), which was dropped at the
2026-08-25 primary→next cutover. The clarify half survived (see
``test_clarify_ping_and_footer_calls.py``); the approval half was omitted
on the reasoning that upstream's ``discord.approval_mentions`` allowlist
broadcast already covered it. That gate defaults to ``False`` and was
never enabled, so exec approvals shipped with no notification at all —
the gate escalated to a human who was never told, and the request ran to
the configured ceiling where the timeout denied it.

Two surfaces, matching the clarify path:

* ``gateway.run._approval_metadata_with_ping`` attaches requester ping
  hints to approval metadata for non-DM chats on platforms with a safe
  plain-text mention syntax.
* Discord's ``send_exec_approval`` renders a validated same-message
  mention; the plain-text fallback prepends the attention prefix.

The requester ping is independent of ``discord.approval_mentions`` — the
allowlist broadcast stays opt-in, and a fragment that only repeats
already-mentioned ids is dropped so the two never double-ping.
"""

from typing import Optional

import pytest

from gateway.config import Platform
from gateway.session import SessionSource


def _source(
    platform: Platform = Platform.DISCORD,
    chat_type: str = "group",
    user_id: Optional[str] = "123456789",
) -> SessionSource:
    return SessionSource(
        platform=platform,
        chat_id="100",
        chat_type=chat_type,
        user_id=user_id,
    )


class TestApprovalMetadataWithPing:
    def test_no_attention_returns_original_object(self):
        from gateway.run import _approval_metadata_with_ping

        original = {"thread_id": "42"}
        assert _approval_metadata_with_ping(original, _source(chat_type="dm")) is original
        assert _approval_metadata_with_ping(original, None) is original
        assert (
            _approval_metadata_with_ping(original, _source(platform=Platform.TELEGRAM))
            is original
        )

    def test_attention_adds_ping_keys_without_mutating(self):
        from gateway.run import _approval_metadata_with_ping

        original = {"thread_id": "42"}
        out = _approval_metadata_with_ping(original, _source())
        assert out is not original
        assert original == {"thread_id": "42"}  # caller's dict untouched
        assert out["approval_ping_user_id"] == "123456789"
        assert out["approval_attention_prefix"] == "<@123456789> "
        assert out["approval_ping_platform"] == "discord"
        assert out["thread_id"] == "42"  # original metadata preserved

    def test_none_metadata_with_attention(self):
        from gateway.run import _approval_metadata_with_ping

        out = _approval_metadata_with_ping(None, _source())
        assert out["approval_ping_user_id"] == "123456789"

    def test_approval_and_clarify_keys_are_distinct(self):
        """One surface must not imply the other in an adapter."""
        from gateway.run import _approval_metadata_with_ping, _clarify_metadata_with_ping

        approval = _approval_metadata_with_ping(None, _source())
        clarify = _clarify_metadata_with_ping(None, _source())
        assert "clarify_ping_user_id" not in approval
        assert "approval_ping_user_id" not in clarify


class TestDiscordApprovalPingContent:
    def test_valid_snowflake_mentions(self):
        from plugins.platforms.discord.adapter import _approval_ping_content

        assert (
            _approval_ping_content({"approval_ping_user_id": "123456789"})
            == "<@123456789> 🔔"
        )

    @pytest.mark.parametrize(
        "metadata",
        [
            None,
            {},
            {"approval_ping_user_id": ""},
            {"approval_ping_user_id": None},
            {"approval_ping_user_id": "not-numeric"},
            {"approval_ping_user_id": "@everyone"},
            {"approval_ping_user_id": "123\n<@456>"},
            {"approval_ping_user_id": "1"},  # too short to be a snowflake
            {"approval_ping_user_id": "1" * 26},  # too long
            {"approval_ping_user_id": "12 34"},
            {"clarify_ping_user_id": "123456789"},  # wrong surface
            {"unrelated": "x"},
        ],
    )
    def test_invalid_metadata_renders_no_mention(self, metadata):
        from plugins.platforms.discord.adapter import _approval_ping_content

        assert _approval_ping_content(metadata) is None


class TestMergeApprovalMentions:
    def test_blank_fragments_collapse_to_none(self):
        from plugins.platforms.discord.adapter import _merge_approval_mentions

        assert _merge_approval_mentions(None, None) is None
        assert _merge_approval_mentions("", None, "   ") is None

    def test_requester_ping_alone(self):
        from plugins.platforms.discord.adapter import _merge_approval_mentions

        assert _merge_approval_mentions("<@123456789> 🔔", None) == "<@123456789> 🔔"

    def test_allowlist_alone_is_preserved(self):
        from plugins.platforms.discord.adapter import _merge_approval_mentions

        assert _merge_approval_mentions(None, "<@1> <@2>") == "<@1> <@2>"

    def test_redundant_allowlist_fragment_is_dropped(self):
        """Allowlist == just the requester must not double-ping."""
        from plugins.platforms.discord.adapter import _merge_approval_mentions

        assert (
            _merge_approval_mentions("<@123456789> 🔔", "<@123456789>")
            == "<@123456789> 🔔"
        )

    def test_additional_allowlist_ids_are_kept(self):
        from plugins.platforms.discord.adapter import _merge_approval_mentions

        out = _merge_approval_mentions("<@123456789> 🔔", "<@123456789> <@999>")
        assert out == "<@123456789> 🔔 <@123456789> <@999>"


class TestApprovalTextFallbackPrefix:
    def test_fallback_body_is_unchanged_by_the_port(self):
        """The prefix is prepended at the callsite, not baked into the formatter."""
        from gateway.run import _format_exec_approval_fallback

        msg = _format_exec_approval_fallback("rm -rf /tmp/x", "destructive", "/")
        assert msg.startswith("⚠️ **Dangerous command requires approval:**")
        assert "<@" not in msg

    def test_prefix_composes_to_a_mentioning_message(self):
        from gateway.run import _blocking_attention_prefix, _format_exec_approval_fallback

        body = _format_exec_approval_fallback("rm -rf /tmp/x", "destructive", "/")
        prefixed = f"{_blocking_attention_prefix(_source())}🔔 {body}"
        assert prefixed.startswith("<@123456789> 🔔 ⚠️")

    def test_dm_composes_without_a_mention(self):
        from gateway.run import _blocking_attention_prefix, _format_exec_approval_fallback

        source = _source(chat_type="dm")
        body = _format_exec_approval_fallback("echo ok", "safe-ish", "/")
        assert _blocking_attention_prefix(source) == ""
        assert "<@" not in f"{_blocking_attention_prefix(source)}{body}"
