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
    return SimpleNamespace(
        type=SimpleNamespace(value=0 if filesize_limit is not None else 1),
        guild=SimpleNamespace(filesize_limit=filesize_limit) if filesize_limit is not None else None,
        send=AsyncMock(return_value=SimpleNamespace(id=42, attachments=[])),
    )


@pytest.mark.asyncio
async def test_oversize_file_refused_before_upload(tmp_path):
    a, mod = _adapter()
    big = tmp_path / "render.pdf"
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    ch = _channel(filesize_limit=1 * 1024 * 1024)
    a._client.get_channel.return_value = ch
    result = await a._send_file_attachment("123", str(big), caption="c")
    assert result.success is False
    assert "upload limit" in result.error
    assert str(big) in result.error
    ch.send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("filesize_limit", [8 * 1024 * 1024, None], ids=["guild", "dm"])
async def test_undersize_file_proceeds(tmp_path, monkeypatch, filesize_limit):
    a, mod = _adapter()
    upload = SimpleNamespace(filename="note.txt")
    file_cls = MagicMock(return_value=upload)
    monkeypatch.setattr(mod.discord, "File", file_cls)
    small = tmp_path / "note.txt"
    small.write_bytes(b"hello")
    sent_msg = SimpleNamespace(id=42, attachments=[SimpleNamespace(filename="note.txt")])
    ch = _channel(filesize_limit)
    ch.send = AsyncMock(return_value=sent_msg)
    a._client.get_channel.return_value = ch
    result = await a._send_file_attachment("123", str(small))
    assert result.success is True
    assert result.message_id == "42"
    ch.send.assert_awaited_once()
    file_cls.assert_called_once_with(str(small), filename="note.txt")
    ch.send.assert_awaited_once_with(content=None, files=[upload])


@pytest.mark.asyncio
async def test_guildless_channel_uses_8mib_floor(tmp_path):
    a, mod = _adapter()
    big = tmp_path / "dm.bin"
    big.write_bytes(b"x" * (9 * 1024 * 1024))
    ch = _channel(None)
    a._client.get_channel.return_value = ch
    result = await a._send_file_attachment("123", str(big))
    assert result.success is False
    assert "8 MiB upload limit" in result.error
    ch.send.assert_not_called()


@pytest.mark.asyncio
async def test_accepted_message_without_attachments_is_not_success(tmp_path, monkeypatch):
    a, mod = _adapter()
    upload = SimpleNamespace(filename="note.txt")
    file_cls = MagicMock(return_value=upload)
    monkeypatch.setattr(mod.discord, "File", file_cls)
    small = tmp_path / "note.txt"
    small.write_bytes(b"hello")
    ch = _channel(8 * 1024 * 1024)
    a._client.get_channel.return_value = ch
    result = await a._send_file_attachment("123", str(small), caption="caption")
    assert result.success is False
    assert result.message_id == "42"
    assert "attached no files" in result.error
    file_cls.assert_called_once_with(str(small), filename="note.txt")
    ch.send.assert_awaited_once_with(content="caption", files=[upload])
