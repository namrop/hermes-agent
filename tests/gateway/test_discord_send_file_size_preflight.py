"""Contract tests: outbound file-attachment size pre-flight (413 class).

_send_file_attachment refuses uploads over the channel's filesize limit
BEFORE hitting Discord, returning a loud SendResult error carrying the
size, the limit, and the local path — instead of a 413 that dies in the
adapter log (55 historical occurrences, large PDF renders).
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _adapter():
    from gateway.config import PlatformConfig
    from plugins.platforms.discord import adapter as discord_adapter

    a = discord_adapter.DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    a._client = MagicMock()
    return a, discord_adapter


def _channel(filesize_limit):
    ch = MagicMock()
    ch.guild = SimpleNamespace(filesize_limit=filesize_limit)
    ch.send = AsyncMock()
    return ch


@pytest.mark.asyncio
async def test_oversize_file_refused_before_upload(tmp_path):
    a, mod = _adapter()
    big = tmp_path / "render.pdf"
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    ch = _channel(filesize_limit=1 * 1024 * 1024)
    a._client.get_channel.return_value = ch
    a._is_forum_parent = lambda c: False
    result = await a._send_file_attachment("123", str(big), caption="c")
    assert result.success is False
    assert "upload limit" in result.error
    assert str(big) in result.error
    ch.send.assert_not_called()


@pytest.mark.asyncio
async def test_undersize_file_proceeds(tmp_path, monkeypatch):
    a, mod = _adapter()
    small = tmp_path / "note.txt"
    small.write_bytes(b"hello")
    sent_msg = MagicMock()
    sent_msg.attachments = [MagicMock()]
    sent_msg.id = 42
    ch = _channel(filesize_limit=8 * 1024 * 1024)
    ch.send = AsyncMock(return_value=sent_msg)
    a._client.get_channel.return_value = ch
    a._is_forum_parent = lambda c: False
    result = await a._send_file_attachment("123", str(small))
    assert result.success is True
    ch.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_guildless_channel_uses_8mib_floor(tmp_path):
    a, mod = _adapter()
    big = tmp_path / "dm.bin"
    big.write_bytes(b"x" * (9 * 1024 * 1024))
    ch = MagicMock()
    ch.guild = None
    ch.send = AsyncMock()
    a._client.get_channel.return_value = ch
    a._is_forum_parent = lambda c: False
    result = await a._send_file_attachment("123", str(big))
    assert result.success is False
    ch.send.assert_not_called()
