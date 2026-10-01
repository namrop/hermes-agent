"""ACP clients (T3 Code) can send audio, and Hermes treats it like a voice note.

Pinned here:

1. ``initialize`` declares ``promptCapabilities.audio`` so clients know they
   may send ``ContentBlock::Audio``.
2. Each audio block is cached, transcribed with the configured STT provider
   (local fallback when that fails) and replaced in place by the gateway's
   wording: the quoted transcript, the empty-audio sentinel, or a note with
   the cached path.
3. The agent receives the transcript as part of the user's message, and the
   client is shown what was heard as a completed tool call unless
   ``stt_echo_transcripts`` is off.
4. A prompt carrying audio is never handled as a slash command.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import acp
from acp.schema import AudioContentBlock, TextContentBlock

from acp_adapter import audio_prompts
from acp_adapter.audio_prompts import (
    EMPTY_TRANSCRIPT_NOTE,
    UNREADABLE_AUDIO_NOTE,
    audio_extension_for_mime,
    decode_audio_data,
    stt_echo_enabled,
    transcribe_audio_blocks,
)
from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager

AUDIO_BYTES = b"OggS\x00fake-voice-note"


def _audio(mime: str = "audio/ogg", data: bytes = AUDIO_BYTES) -> AudioContentBlock:
    return AudioContentBlock(
        type="audio", data=base64.b64encode(data).decode(), mime_type=mime
    )


class _Recorder:
    """Stand-ins for cache / STT that record what they were given."""

    def __init__(self, *, transcript="hello from a voice note", ok=True, fallback=None,
                 enabled=True):
        self.cached: list[tuple[bytes, str]] = []
        self.transcribed: list[str] = []
        self.fallback_calls: list[str] = []
        self._transcript = transcript
        self._ok = ok
        self._fallback = fallback
        self._enabled = enabled

    def cache(self, data: bytes, ext: str) -> str:
        self.cached.append((data, ext))
        return f"/tmp/hermes-audio-cache/audio_{len(self.cached)}{ext}"

    def stt_enabled(self) -> bool:
        return self._enabled

    def transcribe(self, path: str) -> dict:
        self.transcribed.append(path)
        if self._ok:
            return {"success": True, "transcript": self._transcript}
        return {"success": False, "transcript": "", "error": "gateway down"}

    def local_fallback(self, path: str) -> dict:
        self.fallback_calls.append(path)
        if self._fallback is None:
            return {"success": False, "transcript": "", "error": "no local backend"}
        return {"success": True, "transcript": self._fallback}

    def kwargs(self) -> dict:
        return {
            "cache": self.cache,
            "stt_enabled": self.stt_enabled,
            "transcribe": self.transcribe,
            "local_fallback": self.local_fallback,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_extension_follows_the_mime_type():
    assert audio_extension_for_mime("audio/mp4") == ".m4a"
    assert audio_extension_for_mime("audio/x-m4a") == ".m4a"
    assert audio_extension_for_mime("audio/webm;codecs=opus") == ".webm"
    assert audio_extension_for_mime("audio/mpeg") == ".mp3"
    assert audio_extension_for_mime(None) == ".ogg"


def test_decode_accepts_plain_base64_and_data_urls():
    encoded = base64.b64encode(AUDIO_BYTES).decode()
    assert decode_audio_data(encoded) == AUDIO_BYTES
    assert decode_audio_data(f"data:audio/ogg;base64,{encoded}") == AUDIO_BYTES
    assert decode_audio_data("") is None
    assert decode_audio_data(None) is None


def test_echo_switch_reads_both_config_forms_and_defaults_on():
    assert stt_echo_enabled({}) is True
    assert stt_echo_enabled({"stt_echo_transcripts": False}) is False
    assert stt_echo_enabled({"stt": {"echo_transcripts": False}}) is False
    assert stt_echo_enabled({"stt": {"echo_transcripts": "off"}}) is False
    assert stt_echo_enabled({"stt_echo_transcripts": True, "stt": {"echo_transcripts": False}}) is True


# ---------------------------------------------------------------------------
# transcribe_audio_blocks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_without_audio_is_returned_untouched():
    rec = _Recorder()
    prompt = [TextContentBlock(type="text", text="just text")]
    out, transcripts = await transcribe_audio_blocks(prompt, **rec.kwargs())
    assert out is prompt
    assert transcripts == []
    assert rec.cached == []


@pytest.mark.asyncio
async def test_audio_is_cached_transcribed_and_replaced_in_place():
    rec = _Recorder(transcript="open the deploy log")
    prompt = [
        TextContentBlock(type="text", text="[Attached file \"note.m4a\" is saved at: /x]"),
        _audio("audio/mp4"),
    ]
    out, transcripts = await transcribe_audio_blocks(prompt, **rec.kwargs())

    assert rec.cached == [(AUDIO_BYTES, ".m4a")]
    assert rec.transcribed == ["/tmp/hermes-audio-cache/audio_1.m4a"]
    assert rec.fallback_calls == []
    assert [b.text for b in out] == [
        "[Attached file \"note.m4a\" is saved at: /x]",
        '"open the deploy log"',
    ]
    assert all(isinstance(b, TextContentBlock) for b in out)
    assert transcripts == ["open the deploy log"]


@pytest.mark.asyncio
async def test_local_fallback_recovers_a_failed_configured_transcription():
    rec = _Recorder(ok=False, fallback="recovered locally")
    out, transcripts = await transcribe_audio_blocks([_audio()], **rec.kwargs())
    assert rec.fallback_calls == ["/tmp/hermes-audio-cache/audio_1.ogg"]
    assert out[0].text == '"recovered locally"'
    assert transcripts == ["recovered locally"]


@pytest.mark.asyncio
async def test_failed_transcription_points_the_agent_at_the_cached_audio():
    rec = _Recorder(ok=False, fallback=None)
    out, transcripts = await transcribe_audio_blocks([_audio()], **rec.kwargs())
    assert out[0].text == (
        "[voice message could not be transcribed automatically; "
        "the audio is available at: /tmp/hermes-audio-cache/audio_1.ogg]"
    )
    assert transcripts == []


@pytest.mark.asyncio
async def test_transcriber_exception_is_reported_as_a_failed_transcription():
    rec = _Recorder()

    def boom(_path):
        raise RuntimeError("socket closed")

    kwargs = rec.kwargs() | {"transcribe": boom}
    out, transcripts = await transcribe_audio_blocks([_audio()], **kwargs)
    assert out[0].text.startswith("[voice message could not be transcribed automatically;")
    assert transcripts == []


@pytest.mark.asyncio
async def test_empty_transcript_becomes_the_do_not_guess_sentinel():
    rec = _Recorder(transcript="   ")
    out, transcripts = await transcribe_audio_blocks([_audio()], **rec.kwargs())
    assert out[0].text == EMPTY_TRANSCRIPT_NOTE
    assert transcripts == []


@pytest.mark.asyncio
async def test_stt_turned_off_passes_the_cached_path_without_transcribing():
    rec = _Recorder(enabled=False)
    out, transcripts = await transcribe_audio_blocks([_audio()], **rec.kwargs())
    assert rec.transcribed == []
    assert out[0].text == "[The user sent a voice message: /tmp/hermes-audio-cache/audio_1.ogg]"
    assert transcripts == []


@pytest.mark.asyncio
async def test_audio_without_data_is_reported_not_transcribed():
    rec = _Recorder()
    block = AudioContentBlock(type="audio", data="", mime_type="audio/ogg")
    out, transcripts = await transcribe_audio_blocks([block], **rec.kwargs())
    assert rec.cached == []
    assert out[0].text == UNREADABLE_AUDIO_NOTE
    assert transcripts == []


@pytest.mark.asyncio
async def test_cache_refusal_is_reported_not_transcribed():
    rec = _Recorder()

    def too_big(_data, _ext):
        raise ValueError("Inbound audio payload is too large")

    kwargs = rec.kwargs() | {"cache": too_big}
    out, transcripts = await transcribe_audio_blocks([_audio()], **kwargs)
    assert rec.transcribed == []
    assert out[0].text == UNREADABLE_AUDIO_NOTE


@pytest.mark.asyncio
async def test_several_clips_keep_their_order():
    rec = _Recorder()
    out, transcripts = await transcribe_audio_blocks(
        [_audio(), TextContentBlock(type="text", text="between"), _audio("audio/wav")],
        **rec.kwargs(),
    )
    assert [ext for _, ext in rec.cached] == [".ogg", ".wav"]
    assert [b.text for b in out] == [
        '"hello from a voice note"',
        "between",
        '"hello from a voice note"',
    ]
    assert len(transcripts) == 2


# ---------------------------------------------------------------------------
# Server wiring
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_manager():
    return SessionManager(agent_factory=lambda: MagicMock(name="MockAIAgent"))


@pytest.fixture()
def agent(mock_manager):
    return HermesACPAgent(session_manager=mock_manager)


@pytest.mark.asyncio
async def test_initialize_declares_audio_prompts(agent):
    resp = await agent.initialize(protocol_version=acp.PROTOCOL_VERSION)
    caps = resp.agent_capabilities.prompt_capabilities
    assert caps.audio is True
    assert caps.image is True


def _patch_transcription(transcript="voice says hello"):
    async def fake(prompt, **_kwargs):
        out = []
        transcripts = []
        for block in prompt:
            if isinstance(block, AudioContentBlock):
                out.append(TextContentBlock(type="text", text=f'"{transcript}"'))
                transcripts.append(transcript)
            else:
                out.append(block)
        return out, transcripts

    return patch("acp_adapter.server.transcribe_audio_blocks", side_effect=fake)


async def _run_prompt(agent, mock_manager, prompt):
    resp = await agent.new_session(cwd=".")
    state = mock_manager.get_session(resp.session_id)
    seen: dict = {}

    def _run(*args, **kwargs):
        seen.update(kwargs)
        return {"final_response": "ok", "messages": []}

    state.agent.run_conversation = _run
    state.agent.model = "test-model"
    state.agent.provider = "openrouter"
    conn = MagicMock(spec=acp.Client)
    conn.session_update = AsyncMock()
    agent._conn = conn
    await agent.prompt(prompt=prompt, session_id=resp.session_id)
    return seen, conn


def _echo_updates(conn):
    updates = [call.args[1] for call in conn.session_update.await_args_list if len(call.args) > 1]
    return [
        u for u in updates
        if getattr(u, "session_update", None) == "tool_call"
        and getattr(u, "title", None) == "Voice note transcript"
    ]


@pytest.mark.asyncio
async def test_agent_receives_the_transcript_and_the_client_sees_it(agent, mock_manager):
    with _patch_transcription("voice says hello"), patch(
        "acp_adapter.server.stt_echo_enabled", return_value=True
    ):
        seen, conn = await _run_prompt(
            agent,
            mock_manager,
            [TextContentBlock(type="text", text="see the clip"), _audio()],
        )

    assert seen["user_message"] == 'see the clip\n"voice says hello"'
    assert 'voice says hello' in seen["persist_user_message"]
    echoes = _echo_updates(conn)
    assert len(echoes) == 1
    assert echoes[0].status == "completed"
    assert echoes[0].content[0].content.text == '🎙️ "voice says hello"'
    assert echoes[0].field_meta == {"hermes": {"toolName": "voice_note_transcript"}}
    assert echoes[0].raw_output == "voice says hello"


@pytest.mark.asyncio
async def test_echo_off_sends_no_transcript_item(agent, mock_manager):
    with _patch_transcription(), patch(
        "acp_adapter.server.stt_echo_enabled", return_value=False
    ):
        seen, conn = await _run_prompt(agent, mock_manager, [_audio()])
    assert seen["user_message"] == '"voice says hello"'
    assert _echo_updates(conn) == []


@pytest.mark.asyncio
async def test_prompt_with_audio_is_not_treated_as_a_slash_command(agent, mock_manager):
    with _patch_transcription(), patch(
        "acp_adapter.server.stt_echo_enabled", return_value=False
    ), patch.object(agent, "_handle_slash_command", return_value="handled") as slash:
        seen, _conn = await _run_prompt(
            agent,
            mock_manager,
            [TextContentBlock(type="text", text="/help"), _audio()],
        )
    slash.assert_not_called()
    assert seen["user_message"] == '/help\n"voice says hello"'


def test_module_has_no_import_time_side_effects():
    # Heavy imports (gateway cache, STT providers) happen on first use only.
    assert audio_prompts._default_cache.__module__ == "acp_adapter.audio_prompts"
