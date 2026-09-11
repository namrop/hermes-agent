"""``x-opencode-session`` — OpenCode relay session-affinity header.

OpenCode's relay (opencode.ai Zen/Go/free) routes on a per-conversation
session id: requests that share an ``x-opencode-session`` value are pinned to
one upstream backend, which is what keeps its prompt cache warm across the
turns of a conversation. Since the relay began *requiring* the header it
rejects requests without it outright::

    400 MissingSessionID - Request is missing x-opencode-session and cannot
    be routed efficiently.

The value only has to be opaque and stable per conversation, so it is derived
the same way as the other conversation-affinity hints this codebase already
sends (Nous Portal's sticky ``session_id``, xAI's ``x-grok-conv-id``): the
ambient conversation root first, then the caller's rotation-stable scope or
physical session id — normalized through ``_cache_scope_from_session_id`` so
repeat fires of one cron job share a scope.

Every OpenCode request — main turn on any transport, auxiliary calls
(compression, titles, vision, MoA) — goes through
:func:`merge_opencode_session_headers` so the header cannot drift per code
path. Note that ``plugins/model-providers/opencode-zen`` cannot own this: its
``profile.default_headers`` are client-level and this value varies per
conversation.
"""

from __future__ import annotations

from typing import Any, Optional

OPENCODE_SESSION_HEADER = "x-opencode-session"


def is_opencode_target(provider: Optional[str], base_url: Optional[str]) -> bool:
    """True when *provider* or *base_url* addresses the OpenCode relay.

    Matches the built-in opencode-zen/go/free providers, custom
    ``opencode-<family>-*`` providers, and any base_url hosted on opencode.ai.
    """
    try:
        from hermes_cli.models import opencode_provider_family

        if opencode_provider_family(provider) is not None:
            return True
    except Exception:
        pass
    try:
        from agent.anthropic_adapter import _is_opencode_endpoint

        return bool(_is_opencode_endpoint(str(base_url or "")))
    except Exception:
        return False


def opencode_session_headers(
    provider: Optional[str],
    base_url: Optional[str],
    session_id: Optional[str] = None,
) -> dict[str, str]:
    """Return ``{"x-opencode-session": <key>}`` for OpenCode targets, else ``{}``."""
    if not is_opencode_target(provider, base_url):
        return {}
    try:
        from agent.portal_tags import get_conversation_context
        from agent.transports.codex import _cache_scope_from_session_id

        key = _cache_scope_from_session_id(
            get_conversation_context() or session_id
        )
    except Exception:
        key = str(session_id or "")
    return {OPENCODE_SESSION_HEADER: key} if key else {}


def merge_opencode_session_headers(
    kwargs: dict[str, Any],
    provider: Optional[str],
    base_url: Optional[str],
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Merge the affinity header into ``kwargs["extra_headers"]`` (in place).

    Merges rather than assigns, and existing per-request headers win: the
    per-mode builders this wraps already own ``extra_headers`` for their own
    purposes (``anthropic-beta`` on the Messages transport, ``session_id`` /
    ``x-grok-conv-id`` on the Codex transport) and must not be clobbered.
    Non-OpenCode targets are left untouched.
    """
    headers = opencode_session_headers(provider, base_url, session_id)
    if headers:
        existing = kwargs.get("extra_headers")
        merged = dict(existing) if isinstance(existing, dict) else {}
        for key, value in headers.items():
            merged.setdefault(key, value)
        kwargs["extra_headers"] = merged
    return kwargs
