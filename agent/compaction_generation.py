"""How many times this conversation's history has been REWRITTEN.

Hermes appends to a transcript all day and rewrites it rarely: a context
compaction replaces the live history with a summary plus a tail, either in
place (same session id) or by rotating to a continuation child. Everything
downstream that says "this is the same conversation" treats those two cases
alike — correctly, for caching and for attribution.

One consumer must not: a provider that keys a *replayable upstream session*
off the conversation. Such a proxy holds its own copy of the pre-compaction
history; handed the same key after a rewrite, it resumes that copy, matches
the incoming messages against it by suffix overlap and forwards only the
delta — so the compacted prefix never reaches it and the upstream transcript
keeps growing the history Hermes just threw away. (Observed on the deployed
Meridian passthrough: a 208k/168k local context replaying as 986k/995k
upstream under one affinity header.)

The generation is what lets that consumer tell the two apart:

* it advances by exactly one per COMMITTED rewrite — inside the same
  SessionDB transaction that publishes the compacted rows
  (``archive_and_compact``; ``publish_compression_child`` carries the
  parent's count onto the continuation and adds one), so a failed,
  refused, cancelled or lease-lost compaction leaves it where it was;
* it is durable, so a resume, a restart, a fresh process and a rotated child
  all resolve the same number;
* it starts at 0, and generation 0 is defined to produce the pre-feature
  identity — upgrading an install does not invalidate conversations that
  were never compacted.

``prompt_cache_scope`` (the lineage ROOT) deliberately stays stable across a
rewrite: that is a caching identity, and churning it would cost real money.
The two are combined by :mod:`agent.provider_session_affinity`; neither
replaces the other.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, TypeGuard

logger = logging.getLogger(__name__)

#: Fallback counter for agents whose store cannot record the generation.
_IN_MEMORY_ATTR = "_compaction_generation_in_memory"

#: Last valid durable read: ``(store object, session id, generation)``.
_DURABLE_FLOOR_ATTR = "_compaction_generation_durable_floor"


def _durable_reader(agent: Any) -> Optional[Callable[[str], Any]]:
    """The bound ``get_compaction_generation`` for *agent*'s store, or None.

    None means "nothing durable can answer this": no session DB (CLI runs
    with persistence off, throwaway agents) or a duck-typed/plugin session
    store that predates the counter. Those fall back to the in-memory count.
    """
    db = getattr(agent, "_session_db", None)
    if db is None:
        return None
    reader = getattr(db, "get_compaction_generation", None)
    return reader if callable(reader) else None


def _in_memory_generation(agent: Any) -> int:
    value = getattr(agent, _IN_MEMORY_ATTR, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _valid_generation(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _durable_floor(agent: Any, store: Any, session_id: str) -> Optional[int]:
    """Last valid generation observed for this exact store/session pair."""
    memo = getattr(agent, _DURABLE_FLOOR_ATTR, None)
    if not isinstance(memo, tuple) or len(memo) != 3:
        return None
    memo_store, memo_session_id, generation = memo
    if memo_store is not store or memo_session_id != session_id:
        return None
    return generation if _valid_generation(generation) else None


def _remember_durable_generation(
    agent: Any, store: Any, session_id: str, generation: int
) -> int:
    """Advance, but never roll back, the observed generation for one context."""
    known = _durable_floor(agent, store, session_id)
    resolved = max(generation, known) if known is not None else generation
    try:
        setattr(agent, _DURABLE_FLOOR_ATTR, (store, session_id, resolved))
    except Exception:
        # Frozen/slotted test doubles still get the fresh durable answer; they
        # simply cannot retain it across a later transient read failure.
        logger.debug("durable compaction generation not memoizable", exc_info=True)
    return resolved


def resolve_compaction_generation(agent: Any) -> int:
    """Committed-rewrite count for *agent*'s current conversation.

    Reads the durable counter when the agent has a store that keeps one, so
    the answer includes rewrites this agent object never saw — a gateway
    hygiene compaction run through its own agent, a proactive prune committed
    by the context compressor, a compaction in another process. Falls back to
    the in-memory count (see :func:`note_compaction_committed`) when there is
    nothing durable to read, and to 0 when even that is absent. A valid durable
    result is remembered as a monotonic floor for that exact store/session pair:
    a transient exception (or the store's defensive 0 for one) cannot reattach
    an already-compacted conversation to its pre-compaction upstream session.
    The durable row is still read on every call so sibling commits remain visible.

    Never raises: an unreadable store with no prior valid observation means
    "generation 0", which is exactly the pre-feature behavior.
    """
    sid = str(getattr(agent, "session_id", None) or "")
    store = getattr(agent, "_session_db", None)
    reader = getattr(store, "get_compaction_generation", None)
    reader = reader if callable(reader) else None
    if sid and reader is not None:
        floor = _durable_floor(agent, store, sid)
        try:
            value = reader(sid)
        except Exception:
            logger.debug("compaction generation read failed", exc_info=True)
        else:
            if _valid_generation(value):
                return _remember_durable_generation(agent, store, sid, value)
        return floor if floor is not None else 0
    return _in_memory_generation(agent)


def note_compaction_committed(agent: Any) -> None:
    """Record a committed rewrite the durable store could not record itself.

    No-op when the agent HAS a durable counter: that one already advanced
    inside the publishing transaction, and a second count here would make the
    live agent disagree with every reader that loads the session fresh.

    The in-memory counter it maintains otherwise is honest but weaker: it
    counts rewrites committed by THIS agent object in THIS process, so it
    resets on restart and does not see a sibling's compaction. That is the
    best available answer for a conversation with no durable transcript —
    there is no shared state to read, and a stale-but-stable value is the
    pre-feature behavior anyway.
    """
    if _durable_reader(agent) is not None:
        return
    try:
        setattr(agent, _IN_MEMORY_ATTR, _in_memory_generation(agent) + 1)
    except Exception:
        # Frozen/slotted test doubles — the header simply stays put.
        logger.debug("in-memory compaction generation not recordable", exc_info=True)
