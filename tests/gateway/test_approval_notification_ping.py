from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.discord import (
    DiscordAdapter,
    _discord_approval_ping_content,
    _discord_clarify_ping_content,
)
from gateway.run import (
    _approval_attention_prefix,
    _approval_metadata_with_ping,
    _clarify_metadata_with_ping,
    _format_gateway_approval_prompt,
)
from gateway.session import SessionSource


def _source(*, platform=Platform.DISCORD, chat_type="thread", user_id="1511076531601018981"):
    return SessionSource(
        platform=platform,
        chat_id="1508637982549217351",
        chat_type=chat_type,
        user_id=user_id,
        user_name="namrop",
        thread_id="1511465947431702630" if chat_type == "thread" else None,
    )


def _discord_adapter():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))
    adapter._client = MagicMock()
    adapter._allowed_user_ids = {"1511076531601018981"}
    adapter._allowed_role_ids = set()
    return adapter


class CaptureAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="x"), Platform.DISCORD)
        self.sent = []

    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def get_status(self):
        return {"connected": True}

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, metadata))
        return SendResult(success=True, message_id="m1")


def test_approval_attention_prefix_mentions_shared_discord_surface():
    assert _approval_attention_prefix(_source()) == "<@1511076531601018981> "

    msg = _format_gateway_approval_prompt("rm -rf /tmp/example", "destructive test", _source())
    assert msg.startswith("<@1511076531601018981> 🔔 ⚠️")


def test_approval_attention_prefix_omits_dm_and_unknown_platform_mentions():
    assert _approval_attention_prefix(_source(chat_type="dm")) == ""
    assert _format_gateway_approval_prompt("echo ok", "safe-ish", _source(chat_type="dm")).startswith(
        "🔔 ⚠️"
    )

    unknown = _source(platform=Platform.LOCAL, chat_type="group")
    assert _approval_attention_prefix(unknown) == ""
    assert "@namrop" not in _format_gateway_approval_prompt("echo ok", "fallback", unknown)


def test_metadata_helpers_use_distinct_ping_keys():
    approval_meta = _approval_metadata_with_ping({"thread_id": "thread-42"}, _source())
    assert approval_meta == {
        "thread_id": "thread-42",
        "approval_ping_user_id": "1511076531601018981",
        "approval_attention_prefix": "<@1511076531601018981> ",
        "approval_ping_platform": "discord",
    }

    clarify_meta = _clarify_metadata_with_ping({"thread_id": "thread-42"}, _source())
    assert clarify_meta == {
        "thread_id": "thread-42",
        "clarify_ping_user_id": "1511076531601018981",
        "clarify_attention_prefix": "<@1511076531601018981> ",
        "clarify_ping_platform": "discord",
    }


def test_discord_ping_content_rejects_malformed_ids():
    for metadata in [None, {}, {"approval_ping_user_id": ""}, {"approval_ping_user_id": "@everyone"}, {"approval_ping_user_id": "123\n<@456>"}, {"approval_ping_user_id": "1234"}]:
        assert _discord_approval_ping_content(metadata) is None

    assert _discord_approval_ping_content({"approval_ping_user_id": "1511076531601018981"}) == "<@1511076531601018981> 🔔"
    assert _discord_clarify_ping_content({"clarify_ping_user_id": "1511076531601018981"}) == "<@1511076531601018981> 🔔"


@pytest.mark.asyncio
async def test_discord_exec_approval_sends_ping_content_same_message():
    adapter = _discord_adapter()
    channel = MagicMock()
    sent_msg = SimpleNamespace(id=12345)
    channel.send = AsyncMock(return_value=sent_msg)
    adapter._client.get_channel = MagicMock(return_value=channel)

    result = await adapter.send_exec_approval(
        chat_id="9001",
        command="rm -rf /tmp/example",
        session_key="agent:main:discord:thread:9001:2222",
        description="test approval",
        metadata={"thread_id": "2222", "approval_ping_user_id": "1511076531601018981"},
    )

    assert result.success is True
    adapter._client.get_channel.assert_called_once_with(2222)
    kwargs = channel.send.call_args.kwargs
    assert kwargs["content"] == "<@1511076531601018981> 🔔"
    assert "embed" in kwargs
    assert "view" in kwargs


@pytest.mark.asyncio
async def test_discord_clarify_sends_ping_content_same_message():
    adapter = _discord_adapter()
    channel = MagicMock()
    sent_msg = SimpleNamespace(id=67890)
    channel.send = AsyncMock(return_value=sent_msg)
    adapter._client.get_channel = MagicMock(return_value=channel)

    result = await adapter.send_clarify(
        chat_id="9001",
        question="Pick one",
        choices=["A", "B"],
        clarify_id="cidPing",
        session_key="sk-ping",
        metadata={"thread_id": "2222", "clarify_ping_user_id": "1511076531601018981"},
    )

    assert result.success is True
    adapter._client.get_channel.assert_called_once_with(2222)
    kwargs = channel.send.call_args.kwargs
    assert kwargs["content"] == "<@1511076531601018981> 🔔"
    assert "embed" in kwargs
    assert "view" in kwargs


@pytest.mark.asyncio
async def test_base_clarify_fallback_uses_attention_prefix():
    adapter = CaptureAdapter()
    await adapter.send_clarify(
        chat_id="chat",
        question="Need input?",
        choices=None,
        clarify_id="cid",
        session_key="sk",
        metadata={"clarify_attention_prefix": "<@1511076531601018981> "},
    )
    assert adapter.sent[0][1].startswith("<@1511076531601018981> ❓")
