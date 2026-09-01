"""Behavior contract for blocking-prompt requester pings + footer api_calls.

Ported divergence from primary ``995a564b94`` (reduced) and ``88d26e5623``:

* Clarify prompts must notify the person who has to answer them. The
  gateway attaches requester ping hints to clarify metadata for non-DM
  chats on platforms with a safe plain-text mention syntax; the Discord
  adapter renders a validated same-message mention and the default text
  fallback prepends the attention prefix.

  NOTE (2026-08-31): the original port left approval-side pings out on the
  reasoning that upstream's ``send_exec_approval`` mention mechanism
  (``discord.approval_mentions``) already covered that surface. It does
  not — that gate defaults to ``False`` and was never enabled, so the
  approval surface shipped with no notification at all. Approval-side
  pings are now ported alongside the clarify ones; see
  ``test_approval_notification_ping.py``.
* The runtime footer gains the ``api_calls`` field (model API calls made
  during the turn). Primary's ``elapsed`` field is superseded by
  upstream's ``latency``/``turn_seconds``.
"""

import asyncio
from typing import Optional

import pytest

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
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


class TestBlockingAttentionPrefix:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            (None, ""),
            (_source(chat_type="dm"), ""),  # DM: the user is already here
            (_source(user_id=""), ""),  # no requester identity
            (_source(user_id=None), ""),
            (_source(platform=Platform.TELEGRAM), ""),  # no safe plain-text syntax
            (_source(platform=Platform.SLACK), "<@123456789> "),
            (_source(), "<@123456789> "),
            (_source(chat_type="thread"), "<@123456789> "),
        ],
    )
    def test_prefix_matrix(self, source, expected):
        from gateway.run import _blocking_attention_prefix

        assert _blocking_attention_prefix(source) == expected


class TestClarifyMetadataWithPing:
    def test_no_attention_returns_original_object(self):
        from gateway.run import _clarify_metadata_with_ping

        original = {"thread_id": "42"}
        assert _clarify_metadata_with_ping(original, _source(chat_type="dm")) is original
        assert _clarify_metadata_with_ping(original, None) is original

    def test_attention_adds_ping_keys_without_mutating(self):
        from gateway.run import _clarify_metadata_with_ping

        original = {"thread_id": "42"}
        out = _clarify_metadata_with_ping(original, _source())
        assert out is not original
        assert original == {"thread_id": "42"}  # caller's dict untouched
        assert out["clarify_ping_user_id"] == "123456789"
        assert out["clarify_attention_prefix"] == "<@123456789> "
        assert out["clarify_ping_platform"] == "discord"
        assert out["thread_id"] == "42"  # original metadata preserved

    def test_none_metadata_with_attention(self):
        from gateway.run import _clarify_metadata_with_ping

        out = _clarify_metadata_with_ping(None, _source())
        assert out["clarify_ping_user_id"] == "123456789"


class TestDiscordClarifyPingContent:
    def test_valid_snowflake_mentions(self):
        from plugins.platforms.discord.adapter import _clarify_ping_content

        assert _clarify_ping_content({"clarify_ping_user_id": "123456789"}) == "<@123456789> 🔔"

    @pytest.mark.parametrize(
        "metadata",
        [
            None,
            {},
            {"clarify_ping_user_id": ""},
            {"clarify_ping_user_id": None},
            {"clarify_ping_user_id": "not-numeric"},
            {"clarify_ping_user_id": "@everyone"},
            {"clarify_ping_user_id": "1"},  # too short to be a snowflake
            {"clarify_ping_user_id": "1" * 26},  # too long
            {"clarify_ping_user_id": "12 34"},
            {"unrelated": "x"},
        ],
    )
    def test_invalid_metadata_renders_no_mention(self, metadata):
        from plugins.platforms.discord.adapter import _clarify_ping_content

        assert _clarify_ping_content(metadata) is None


class _StubAdapter(BasePlatformAdapter):
    """Minimal concrete adapter capturing sends for assertions."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return "stub"

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content))
        return SendResult(success=True)

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def stop_typing_for_chat(self, chat_id):
        return None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> dict:
        return {}


class TestBaseAdapterAttentionPrefix:
    """The default text fallback prepends the attention prefix."""

    def _clarify(self, metadata):
        adapter = _StubAdapter()
        asyncio.run(
            adapter.send_clarify(
                chat_id="c1",
                question="Which color?",
                choices=None,
                clarify_id="abc123",
                session_key="k",
                metadata=metadata,
            )
        )
        return adapter.sent[0][1]

    def test_open_ended_question_carries_prefix(self):
        text = self._clarify({"clarify_attention_prefix": "<@9> "})
        assert text.startswith("<@9> ❓ Which color?")

    def test_numbered_choices_carry_prefix(self):
        adapter = _StubAdapter()
        asyncio.run(
            adapter.send_clarify(
                chat_id="c1",
                question="Which color?",
                choices=["red", "blue"],
                clarify_id="abc123",
                session_key="k",
                metadata={"clarify_attention_prefix": "<@9> "},
            )
        )
        assert adapter.sent[0][1].splitlines()[0] == "<@9> ❓ Which color?"

    def test_no_prefix_renders_identically(self):
        text = self._clarify(None)
        assert text.startswith("❓ Which color?")


class TestFooterApiCalls:
    def test_api_calls_renders(self):
        from gateway.runtime_footer import format_runtime_footer

        out = format_runtime_footer(
            model="m",
            context_tokens=0,
            context_length=None,
            cwd="",
            api_calls=12,
            fields=("api_calls",),
        )
        assert out == "12 calls"

    def test_calls_alias_renders(self):
        from gateway.runtime_footer import format_runtime_footer

        out = format_runtime_footer(
            model="m",
            context_tokens=0,
            context_length=None,
            cwd="",
            api_calls=3,
            fields=("calls",),
        )
        assert out == "3 calls"

    def test_api_calls_skipped_when_untracked(self):
        from gateway.runtime_footer import format_runtime_footer

        out = format_runtime_footer(
            model="m",
            context_tokens=0,
            context_length=None,
            cwd="",
            api_calls=None,
            fields=("api_calls",),
        )
        assert out == ""

    def test_api_calls_skipped_when_negative(self):
        from gateway.runtime_footer import format_runtime_footer

        out = format_runtime_footer(
            model="m",
            context_tokens=0,
            context_length=None,
            cwd="",
            api_calls=-1,
            fields=("api_calls",),
        )
        assert out == ""

    def test_latency_plus_api_calls_compose(self):
        from gateway.runtime_footer import format_runtime_footer

        out = format_runtime_footer(
            model="m",
            context_tokens=0,
            context_length=None,
            cwd="",
            turn_seconds=22.0,
            api_calls=7,
            fields=("latency", "api_calls"),
        )
        assert out == "22s · 7 calls"

    def test_api_calls_not_in_default_fields(self):
        from gateway.runtime_footer import _DEFAULT_FIELDS

        assert "api_calls" not in _DEFAULT_FIELDS
