"""Tests for Matrix voice message support (MSC3245).

Updated for the mautrix-python SDK (no more matrix-nio / nio imports).
"""
import os
import tempfile
import types
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Try importing mautrix; skip entire file if not available.
try:
    import mautrix as _mautrix_probe
    if not isinstance(_mautrix_probe, types.ModuleType) or not hasattr(_mautrix_probe, "__file__"):
        pytest.skip("mautrix in sys.modules is a mock, not the real package", allow_module_level=True)
except ImportError:
    pytest.skip("mautrix not installed", allow_module_level=True)

from gateway.platforms.base import MessageType


# ---------------------------------------------------------------------------
# Adapter helpers
# ---------------------------------------------------------------------------

def _make_adapter():
    """Create a MatrixAdapter with mocked config.

    Pins ``require_mention: False`` so these media-detection tests are NOT
    gated by the mention requirement. The adapter defaults require_mention to
    True (falling back to the MATRIX_REQUIRE_MENTION env var), so without this
    a group-room audio event with no @mention is dropped by
    _resolve_message_context before dispatch — making the tests pass or fail
    depending on leaked env state from other tests in the same shard. These
    tests exercise voice/audio TYPE detection, not mention gating.
    """
    from plugins.platforms.matrix.adapter import MatrixAdapter
    from gateway.config import PlatformConfig

    config = PlatformConfig(
        enabled=True,
        token="***",
        extra={
            "homeserver": "https://matrix.example.org",
            "user_id": "@bot:example.org",
            "require_mention": False,
        },
    )
    adapter = MatrixAdapter(config)
    return adapter


def _make_audio_event(
    event_id: str = "$audio_event",
    sender: str = "@alice:example.org",
    room_id: str = "!test:example.org",
    body: str = "Voice message",
    url: str = "mxc://example.org/abc123",
    is_voice: bool = False,
    mimetype: str = "audio/ogg",
    timestamp: int = 9999999999000,  # ms
):
    """
    Create a mock mautrix room message event.

    In mautrix, the handler receives a single event object with attributes
    ``room_id``, ``sender``, ``event_id``, ``timestamp``, and ``content``
    (a dict-like or serializable object).

    Args:
        is_voice: If True, adds org.matrix.msc3245.voice field to content.
    """
    content = {
        "msgtype": "m.audio",
        "body": body,
        "url": url,
        "info": {
            "mimetype": mimetype,
        },
    }

    if is_voice:
        content["org.matrix.msc3245.voice"] = {}

    event = SimpleNamespace(
        event_id=event_id,
        sender=sender,
        room_id=room_id,
        timestamp=timestamp,
        content=content,
    )
    return event


def _make_state_store(member_count: int = 2):
    """Create a mock state store with get_members/get_member support."""
    store = MagicMock()
    # get_members returns a list of member user IDs
    members = [MagicMock() for _ in range(member_count)]
    store.get_members = AsyncMock(return_value=members)
    # get_member returns a single member info object
    member = MagicMock()
    member.displayname = "Alice"
    store.get_member = AsyncMock(return_value=member)
    return store


# ---------------------------------------------------------------------------
# Tests: MSC3245 Voice Detection
# ---------------------------------------------------------------------------

class TestMatrixVoiceMessageDetection:
    """Test that MSC3245 voice messages are detected and tagged correctly."""

    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._user_id = "@bot:example.org"
        self.adapter._startup_ts = 0.0
        self.adapter._dm_rooms = {}
        self.adapter._message_handler = AsyncMock()
        # Mock _mxc_to_http to return a fake HTTP URL
        self.adapter._mxc_to_http = lambda url: f"https://matrix.example.org/_matrix/media/v3/download/{url[6:]}"
        # Mock client for authenticated download — download_media returns bytes directly
        self.adapter._client = MagicMock()
        self.adapter._client.download_media = AsyncMock(return_value=b"fake audio data")
        # State store for DM detection
        self.adapter._client.state_store = _make_state_store()


    @pytest.mark.asyncio
    async def test_voice_message_has_local_path(self):
        """Voice messages should have a local cached path in media_urls."""
        event = _make_audio_event(is_voice=True)

        captured_event = None

        async def capture(msg_event):
            nonlocal captured_event
            captured_event = msg_event

        self.adapter.handle_message = capture

        await self.adapter._on_room_message(event)

        assert captured_event is not None
        assert captured_event.media_urls is not None
        assert len(captured_event.media_urls) > 0
        # Should be a local path, not an HTTP URL
        assert not captured_event.media_urls[0].startswith("http"), \
            f"media_urls should contain local path, got {captured_event.media_urls[0]}"
        # download_media is called with a ContentURI wrapping the mxc URL
        self.adapter._client.download_media.assert_awaited_once()
        assert captured_event.media_types == ["audio/ogg"]


class TestMatrixVoiceCacheFallback:
    """Test graceful fallback when voice caching fails."""

    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._user_id = "@bot:example.org"
        self.adapter._startup_ts = 0.0
        self.adapter._dm_rooms = {}
        self.adapter._message_handler = AsyncMock()
        self.adapter._mxc_to_http = lambda url: f"https://matrix.example.org/_matrix/media/v3/download/{url[6:]}"
        self.adapter._client = MagicMock()
        self.adapter._client.state_store = _make_state_store()

    @pytest.mark.asyncio
    async def test_voice_cache_failure_falls_back_to_http_url(self):
        """If caching fails (download returns None), voice message should still be delivered with HTTP URL."""
        event = _make_audio_event(is_voice=True)

        # download_media returns None on failure
        self.adapter._client.download_media = AsyncMock(return_value=None)

        captured_event = None

        async def capture(msg_event):
            nonlocal captured_event
            captured_event = msg_event

        self.adapter.handle_message = capture

        await self.adapter._on_room_message(event)

        assert captured_event is not None
        assert captured_event.media_urls is not None
        # Should fall back to HTTP URL
        assert captured_event.media_urls[0].startswith("http"), \
            f"Should fall back to HTTP URL on cache failure, got {captured_event.media_urls[0]}"


# ---------------------------------------------------------------------------
# Tests: send_voice includes MSC3245 field
# ---------------------------------------------------------------------------

class TestMatrixSendVoiceMSC3245:
    """Test that send_voice includes MSC3245 field for native voice rendering."""

    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._user_id = "@bot:example.org"
        # Mock client — upload_media returns a ContentURI string
        self.adapter._client = MagicMock()
        self.upload_call = None

        async def mock_upload_media(data, mime_type=None, filename=None, **kwargs):
            self.upload_call = {"data": data, "mime_type": mime_type, "filename": filename}
            return "mxc://example.org/uploaded"

        self.adapter._client.upload_media = mock_upload_media


    @pytest.mark.asyncio
    @pytest.mark.parametrize("mime_lookup", ["native", None, "application/ogg"])
    async def test_send_voice_transcodes_non_ogg_to_opus(self, monkeypatch, mime_lookup):
        """Non-Ogg audio reaching send_voice (e.g. direct text_to_speech MP3)
        is transcoded to Ogg/Opus at the adapter boundary (issue #14841)."""
        if mime_lookup != "native":
            monkeypatch.setattr(
                "plugins.platforms.matrix.adapter.mimetypes.guess_type",
                lambda filename: (mime_lookup, None),
            )
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(b"fake mp3 data")
            temp_path = f.name
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"fake ogg opus data")
            converted_path = f.name

        try:
            sent_content = None

            async def mock_send_message_event(room_id, event_type, content):
                nonlocal sent_content
                sent_content = content
                return "$sent_event"

            self.adapter._client.send_message_event = mock_send_message_event

            with patch(
                "plugins.platforms.matrix.adapter._matrix_transcode_voice_to_ogg",
                return_value=converted_path,
            ) as mock_transcode, patch(
                "plugins.platforms.matrix.adapter._matrix_voice_metadata_for_file",
                return_value={"duration": 1234, "waveform": [0, 512, 1024]},
            ):
                await self.adapter.send_voice(
                    chat_id="!room:example.org",
                    audio_path=temp_path,
                    caption="Test voice",
                )

            mock_transcode.assert_called_once_with(temp_path)
            assert sent_content is not None, "No message was sent"
            assert "org.matrix.msc3245.voice" in sent_content
            assert sent_content["info"]["mimetype"] == "audio/ogg"
            assert self.upload_call is not None
            assert self.upload_call["data"] == b"fake ogg opus data"
            assert self.upload_call["mime_type"] == "audio/ogg"
            assert self.upload_call["filename"].endswith(".ogg")
            # converted temp file is cleaned up by send_voice
            assert not os.path.exists(converted_path)

        finally:
            os.unlink(temp_path)
            if os.path.exists(converted_path):
                os.unlink(converted_path)

    @pytest.mark.asyncio
    async def test_failed_transcode_keeps_mp3_media_type(self, tmp_path):
        """An actual helper failure sends the original bytes and MIME, not Ogg."""
        audio = tmp_path / "voice.mp3"
        audio.write_bytes(b"original mp3")
        self.adapter._client.send_message_event = AsyncMock(return_value="$sent")
        with patch("plugins.platforms.matrix.adapter.shutil.which", return_value="/fake/ffmpeg"), \
             patch("plugins.platforms.matrix.adapter.subprocess.run", return_value=SimpleNamespace(returncode=1)) as ffmpeg, \
             patch("plugins.platforms.matrix.adapter._matrix_voice_metadata_for_file", return_value={}):
            result = await self.adapter.send_voice("!room:example.org", str(audio))
        assert result.success
        ffmpeg.assert_called_once()
        assert not os.path.exists(ffmpeg.call_args.args[0][-1])
        content = self.adapter._client.send_message_event.call_args.args[2]
        assert content["info"]["mimetype"] == "audio/mpeg"
        assert self.upload_call is not None
        assert self.upload_call["mime_type"] == "audio/mpeg"
        assert self.upload_call["data"] == b"original mp3"
        assert self.upload_call["filename"] == "voice.mp3"
        assert audio.read_bytes() == b"original mp3"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("extension", ["ogg", "oga", "opus"])
    async def test_ogg_voice_without_host_mime_database(self, tmp_path, extension):
        audio = tmp_path / f"voice.{extension}"
        audio.write_bytes(b"ogg opus")
        self.adapter._client.send_message_event = AsyncMock(return_value="$sent")
        with patch("plugins.platforms.matrix.adapter.mimetypes.guess_type", return_value=(None, None)), \
             patch("plugins.platforms.matrix.adapter._matrix_transcode_voice_to_ogg") as transcode, \
             patch("plugins.platforms.matrix.adapter._matrix_voice_metadata_for_file", return_value={}):
            result = await self.adapter.send_voice("!room:example.org", str(audio))
        assert result.success
        transcode.assert_not_called()
        assert self.upload_call is not None
        assert self.upload_call["mime_type"] == "audio/ogg"
        assert self.adapter._client.send_message_event.call_args.args[2]["info"]["mimetype"] == "audio/ogg"
        assert audio.exists()

    @pytest.mark.asyncio
    async def test_document_ogg_keeps_general_mime_lookup(self, tmp_path):
        document = tmp_path / "document.ogg"
        document.write_bytes(b"non-voice ogg")
        self.adapter._client.send_message_event = AsyncMock(return_value="$sent")
        with patch("plugins.platforms.matrix.adapter.mimetypes.guess_type", return_value=("application/ogg", None)):
            result = await self.adapter.send_document("!room:example.org", str(document))
        assert result.success
        assert self.upload_call is not None
        assert self.upload_call["mime_type"] == "application/ogg"
        content = self.adapter._client.send_message_event.call_args.args[2]
        assert content["info"]["mimetype"] == "application/ogg"
        assert "org.matrix.msc3245.voice" not in content

