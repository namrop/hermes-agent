"""Turn ACP audio prompt blocks into the text the gateway gives voice notes.

ACP lets a client attach audio to a prompt (``ContentBlock::Audio``: base64
``data`` plus ``mimeType``) once the agent declares
``promptCapabilities.audio``. Hermes's models take text, so each clip is
handled the way the messaging gateway handles a voice note
(``GatewayRunner._enrich_message_with_transcription``):

1. the bytes are cached in Hermes's audio cache, so the agent can reach the
   file later;
2. the configured STT provider transcribes it, with an already-installed
   local backend as the fallback;
3. the audio block is replaced, in place, by a text block worded as the
   gateway words it:

   - a transcript: the transcript as a plain quoted line;
   - no words: a sentinel telling the agent not to guess;
   - a failed transcription: a note giving the cached audio path;
   - STT turned off in config: a note giving the cached audio path.

``stt.echo_transcripts`` (default on, shared with the gateway) decides
whether the client is also shown what was heard; the server sends that as a
completed tool call so it stays out of the agent's reply text.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
from typing import Any, Callable, Optional

from acp.schema import AudioContentBlock, TextContentBlock

logger = logging.getLogger(__name__)

# Same wording as gateway/run.py, so a voice note reads the same to the agent
# whichever surface it came through.
EMPTY_TRANSCRIPT_NOTE = (
    "[The user sent a voice message but it came through empty or inaudible — "
    "speech-to-text returned no words. Do not guess at the content; ask the "
    "user to resend or type it out.]"
)
UNREADABLE_AUDIO_NOTE = "[The user sent a voice message, but its audio could not be read.]"

_MIME_EXTENSIONS = {
    "audio/aac": ".aac",
    "audio/x-aac": ".aac",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mpga": ".mp3",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/mp4a-latm": ".m4a",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "audio/x-caf": ".caf",
}


def audio_extension_for_mime(mime_type: str | None) -> str:
    """Cache-file extension for an audio MIME type; the cache sniffs bytes too."""
    base = (mime_type or "").split(";", 1)[0].strip().lower()
    return _MIME_EXTENSIONS.get(base, ".ogg")


def decode_audio_data(data: str | None) -> bytes | None:
    """Decode an ACP audio block's base64 ``data`` (a data: URL is accepted)."""
    raw = (data or "").strip()
    if raw.startswith("data:"):
        _, _, raw = raw.partition(",")
    if not raw:
        return None
    try:
        decoded = base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError):
        return None
    return decoded or None


def stt_echo_enabled(config: Optional[dict] = None) -> bool:
    """Read the gateway's ``stt_echo_transcripts`` switch (default on)."""
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config() or {}
        except Exception:
            return True
    value = config.get("stt_echo_transcripts")
    if value is None and isinstance(config.get("stt"), dict):
        value = config["stt"].get("echo_transcripts")
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _default_cache(data: bytes, ext: str) -> str:
    from gateway.platforms.base import cache_audio_from_bytes

    return cache_audio_from_bytes(data, ext)


def _default_stt_enabled() -> bool:
    from tools.transcription_tools import is_stt_enabled

    return is_stt_enabled()


def _default_transcribe(path: str) -> dict[str, Any]:
    from tools.transcription_tools import transcribe_audio

    return transcribe_audio(path, None, "acp")


def _default_local_fallback(path: str) -> dict[str, Any]:
    from tools.transcription_tools import transcribe_audio_local_fallback

    return transcribe_audio_local_fallback(path)


def _agent_visible(path: str) -> str:
    try:
        from tools.credential_files import to_agent_visible_cache_path

        return to_agent_visible_cache_path(os.path.abspath(path))
    except Exception:
        return os.path.abspath(path)


def _failed_note(path: str) -> str:
    return (
        "[voice message could not be transcribed automatically; "
        f"the audio is available at: {_agent_visible(path)}]"
    )


async def transcribe_audio_blocks(
    prompt: list[Any],
    *,
    cache: Callable[[bytes, str], str] = _default_cache,
    stt_enabled: Callable[[], bool] = _default_stt_enabled,
    transcribe: Callable[[str], dict[str, Any]] = _default_transcribe,
    local_fallback: Callable[[str], dict[str, Any]] = _default_local_fallback,
) -> tuple[list[Any], list[str]]:
    """Replace every audio block with text; return the prompt and the transcripts.

    Blocks keep their order. Only audio blocks change. The returned
    transcripts are the non-empty ones, in prompt order, for echoing.
    """
    if not any(isinstance(block, AudioContentBlock) for block in prompt):
        return prompt, []

    enabled: bool | None = None
    out: list[Any] = []
    transcripts: list[str] = []
    for block in prompt:
        if not isinstance(block, AudioContentBlock):
            out.append(block)
            continue

        data = decode_audio_data(getattr(block, "data", None))
        if data is None:
            out.append(TextContentBlock(type="text", text=UNREADABLE_AUDIO_NOTE))
            continue
        try:
            path = await asyncio.to_thread(
                cache, data, audio_extension_for_mime(getattr(block, "mime_type", None))
            )
        except Exception as exc:
            logger.info("ACP audio block could not be cached: %s", exc)
            out.append(TextContentBlock(type="text", text=UNREADABLE_AUDIO_NOTE))
            continue

        if enabled is None:
            try:
                enabled = bool(stt_enabled())
            except Exception:
                enabled = True
        if not enabled:
            out.append(
                TextContentBlock(
                    type="text",
                    text=f"[The user sent a voice message: {_agent_visible(path)}]",
                )
            )
            continue

        try:
            result = await asyncio.to_thread(transcribe, path)
            if not result.get("success"):
                fallback = await asyncio.to_thread(local_fallback, path)
                if fallback.get("success"):
                    logger.info(
                        "Configured STT failed for %s; recovered with local STT", path
                    )
                    result = fallback
        except Exception as exc:
            logger.error("ACP voice transcription error: %s", exc)
            out.append(TextContentBlock(type="text", text=_failed_note(path)))
            continue

        if not result.get("success"):
            logger.info(
                "ACP voice transcription failed for %s: %s",
                path,
                result.get("error", "unknown error"),
            )
            out.append(TextContentBlock(type="text", text=_failed_note(path)))
            continue

        transcript = str(result.get("transcript") or "")
        if not transcript.strip():
            out.append(TextContentBlock(type="text", text=EMPTY_TRANSCRIPT_NOTE))
            continue
        transcripts.append(transcript)
        out.append(TextContentBlock(type="text", text=f'"{transcript}"'))

    return out, transcripts
