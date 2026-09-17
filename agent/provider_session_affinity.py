"""Opt-in, provider-scoped conversation affinity header.

A provider entry may name ONE request header that Hermes fills with a stable,
opaque id for the conversation the request belongs to::

    providers:
      meridian-yugen:
        api: http://127.0.0.1:3457
        session_id_header: x-litellm-session-id   # default: off

Why a per-provider opt-in rather than a global header: the header only means
something to a proxy that routes on it. The deployed Meridian passthrough
adapter reads ``x-litellm-session-id`` and nothing else — a request arriving
without it is classified ``independent-request:headerless-tool-result`` and
gets a fresh upstream SDK session (full-history replay). Every other provider
would either ignore the header or reject it, so it stays off unless a provider
asks for it by name.

**The value.** Derived from the agent's rotation-stable prompt-cache scope
(``agent.prompt_cache_scope``) — the compression-lineage ROOT of the physical
session id — then hashed:

    hermes-<first 32 hex of sha256(scope)>

Consequences of that choice, each one load-bearing:

* stable across the turns and tool rounds of a conversation, across retries,
  across agent re-instantiation/resume, and across context-compression
  session rotation (the lineage root does not move when a rotation mints a new
  physical id);
* distinct for ``/new``, ``/branch`` forks, delegate subagents, independent
  cron fires and unrelated sessions, because each resolved scope is hashed
  directly rather than normalized as a prompt-cache key;
* opaque — the provider never sees a Hermes session id, a platform chat id or
  a user id. The digest is unsalted by design: resume in a *new process* must
  land on the same upstream session, so a per-process salt would break the one
  property the header exists for.

NOT ``portal_tags.get_conversation_context()``, which the OpenCode affinity
header prefers: that is the Portal-attribution walk, which follows
``parent_session_id`` blindly and collapses ``/branch`` children and whole
delegate trees into the parent's id. For routing/cache affinity that would pin
a subagent's traffic to its parent's upstream session — exactly the lane
conflation this header must avoid.

**The scope.** Resolution is guarded by identity AND route
(:func:`hermes_cli.config.get_custom_provider_session_id_header`): the opt-in
belongs to one configured provider on one normalized endpoint, so it does not
leak to a sibling provider that shares a base_url, and it is dropped the
moment a fallback or ``/model`` switch moves the agent onto another route.

**Where it is applied.** ``chat_completion_helpers.build_api_kwargs`` — after
the per-mode builder, like the OpenCode header, because the Anthropic
transport assigns ``extra_headers`` outright for ``anthropic-beta`` and the
Codex transport rebuilds it for ``session_id`` / ``x-grok-conv-id``. Merging
there covers every transport and both the streaming and non-streaming paths,
which share one kwargs dict. Auxiliary traffic (compression, titles, vision)
is deliberately NOT tagged: those calls carry a different message list, and
pinning them to the conversation's upstream session would feed foreign history
to a proxy that replays it.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Prefix on every derived value, so the id is recognizably ours in a proxy log.
VALUE_PREFIX = "hermes-"

_MEMO_ATTR = "_provider_session_affinity_memo"


def session_affinity_value(scope: Optional[str]) -> str:
    """Return the opaque affinity id for a logical conversation *scope*.

    Empty scope → empty string (the caller then sends no header). Hash the
    resolved scope directly so independent cron fires remain independent.
    Compression rotation stays stable because the agent-level resolver supplies
    the lineage root before calling this function.
    """
    normalized = str(scope or "")
    if not normalized:
        return ""
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{VALUE_PREFIX}{digest[:32]}"


def session_affinity_value_for_agent(agent: Any) -> str:
    """Resolve the affinity id for *agent*'s current conversation."""
    scope = None
    try:
        from agent.prompt_cache_scope import resolve_prompt_cache_scope_safe

        scope = resolve_prompt_cache_scope_safe(agent)
    except Exception:
        logger.debug("session-affinity scope resolution failed", exc_info=True)
    return session_affinity_value(scope or getattr(agent, "session_id", None))


def resolve_session_id_header(
    provider: Optional[str],
    base_url: Optional[str],
    config: Optional[Dict[str, Any]] = None,
    custom_providers: Optional[list] = None,
) -> Optional[str]:
    """Return the header name *provider* opted into on *base_url*, or ``None``.

    Never raises: a config that cannot be read means "not opted in", which is
    the pre-feature behavior for every provider.
    """
    try:
        from hermes_cli.config import get_custom_provider_session_id_header

        return get_custom_provider_session_id_header(
            provider, str(base_url or ""), custom_providers, config
        )
    except Exception:
        logger.debug("session_id_header resolution failed", exc_info=True)
        return None


def agent_route(agent: Any) -> str:
    """The endpoint *agent* actually sends to, per transport."""
    if getattr(agent, "api_mode", "") == "anthropic_messages":
        anthropic_url = getattr(agent, "_anthropic_base_url", None)
        if anthropic_url:
            return str(anthropic_url)
    return str(getattr(agent, "base_url", "") or "")


def agent_identity(agent: Any) -> str:
    """The provider identity to match config against.

    ``requested_provider`` first: it carries the ``custom:<key>`` spelling the
    user selected, and it is re-pointed by fallback, ``/model`` switch and
    runtime restore, so it never goes stale behind ``provider``.
    """
    return str(
        getattr(agent, "requested_provider", "") or getattr(agent, "provider", "") or ""
    )


def resolve_session_id_header_for_agent(agent: Any) -> Optional[str]:
    """Memoized :func:`resolve_session_id_header` for *agent*'s live route.

    The memo key is (identity, route), so a fallback or ``/model`` switch
    re-resolves instead of carrying the old provider's opt-in onto the new
    endpoint. Config reads are cheap but not free, and this runs once per API
    call.
    """
    key = (agent_identity(agent).strip().lower(), agent_route(agent))
    memo = getattr(agent, _MEMO_ATTR, None)
    if isinstance(memo, tuple) and len(memo) == 2 and memo[0] == key:
        return memo[1]
    header = resolve_session_id_header(key[0], key[1])
    try:
        setattr(agent, _MEMO_ATTR, (key, header))
    except Exception:
        # Frozen/slotted test doubles — resolution still works, unmemoized.
        pass
    return header


def merge_session_affinity_headers(
    kwargs: Dict[str, Any], header: Optional[str], value: Optional[str]
) -> Dict[str, Any]:
    """Merge ``{header: value}`` into ``kwargs["extra_headers"]`` (in place).

    Merges rather than assigns, and an existing value for the same header
    wins: the per-mode builders own ``extra_headers`` for their own purposes
    (``anthropic-beta``, ``x-grok-conv-id``) and a caller that pinned this
    header explicitly means it. No-op when nothing opted in.
    """
    if not header or not value:
        return kwargs
    existing = kwargs.get("extra_headers")
    merged = dict(existing) if isinstance(existing, dict) else {}
    if not any(str(name).lower() == header.lower() for name in merged):
        merged[header] = value
    kwargs["extra_headers"] = merged
    return kwargs


def merge_agent_session_affinity(kwargs: Dict[str, Any], agent: Any) -> Dict[str, Any]:
    """Apply *agent*'s opted-in affinity header to a built request.

    The one entry point the request builder calls. No-op — not even a config
    read beyond the memo — for providers that never opted in.
    """
    try:
        header = resolve_session_id_header_for_agent(agent)
        if not header:
            return kwargs
        return merge_session_affinity_headers(
            kwargs, header, session_affinity_value_for_agent(agent)
        )
    except Exception:
        logger.debug("session affinity header skipped", exc_info=True)
        return kwargs
