"""Abstract base class for pluggable memory providers.

Memory providers give the agent persistent recall across sessions.
The MemoryManager enforces a one-external-provider limit to prevent
tool schema bloat and conflicting memory backends.

External providers (Honcho, Hindsight, Mem0, etc.) are registered
and managed via MemoryManager. Only one external provider runs at a
time.

Registration:
  Plugins ship in plugins/memory/<name>/ and are activated via
  the memory.provider config key.

Lifecycle (called by MemoryManager, wired in run_agent.py):
  initialize()          — connect, create resources, warm up
  system_prompt_block()  — static text for the system prompt
  prefetch(query)        — background recall before each turn
  sync_turn(user, asst)  — async write after each turn
  get_tool_schemas()     — tool schemas to expose to the model
  handle_tool_call()     — dispatch a tool call
  shutdown()             — clean exit

Optional hooks (override to opt in):
  on_turn_start(turn, message, **kwargs) — per-turn tick with runtime context
  on_session_end(messages)               — end-of-session extraction
  on_session_switch(new_session_id, **kwargs) — mid-process session_id rotation
  on_pre_compress(messages) -> str       — extract before context compression
  on_memory_write(action, target, content, metadata=None) — mirror built-in memory writes
  on_delegation(task, result, **kwargs)  — parent-side observation of subagent work
  backup_paths() -> list[str]            — extra on-disk paths to include in `hermes backup`
"""

from __future__ import annotations

import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default glyph for the deterministic memory indicators. Providers override
# per-status with their own brand mark (e.g. Hindsight uses "👁️").
INDICATOR_GLYPH = "🧠"

# Typed recall outcomes. ``recalled`` and ``no_hits`` are successful
# retrievals (content injected / nothing relevant); the others describe why
# automatic recall did NOT run to completion, so the UI can say "skipped" or
# "timed out" instead of silently presenting a failure as "no remembered
# facts" (the 2026-09-17 gateway-freeze scar).
RECALL_OUTCOME_RECALLED = "recalled"
RECALL_OUTCOME_NO_HITS = "no_hits"
RECALL_OUTCOME_SKIPPED = "skipped"
RECALL_OUTCOME_TIMED_OUT = "timed_out"
RECALL_OUTCOME_CANCELLED = "cancelled"
RECALL_OUTCOME_ERROR = "error"


@dataclass(frozen=True)
class RecallStatus:
    """Summary of what a provider's most recent prefetch injected this turn.

    Returned by :meth:`MemoryProvider.recall_status` so the agent can emit a
    deterministic, model-independent "memory was used" indicator (see
    ``MemoryManager.describe_recall``). ``count`` is the number of discrete
    memories injected; ``0`` means content was injected but has no discrete
    count (e.g. a synthesized reflect answer), which the indicator renders
    generically rather than as "0 memories". ``glyph`` is the brand mark the
    indicator leads with.

    ``outcome`` types the result (see ``RECALL_OUTCOME_*``). The default,
    ``recalled``, keeps every existing positional/keyword construction
    unchanged. Providers that can distinguish "nothing matched" from "the
    retrieval was skipped / timed out / cancelled" set it accordingly and put
    a short human-readable cause in ``detail`` (never the query text).
    """

    provider_label: str
    count: int
    glyph: str = INDICATOR_GLYPH
    outcome: str = RECALL_OUTCOME_RECALLED
    detail: str = ""


class RecallInterrupted(Exception):
    """Base for typed retrieval interruptions raised out of ``prefetch``.

    ``MemoryManager`` records the ``outcome`` as the provider's typed prefetch
    result instead of treating it as an opaque provider failure.
    """

    outcome = RECALL_OUTCOME_CANCELLED


class RecallCancelled(RecallInterrupted):
    """The caller cancelled the retrieval (e.g. manager timeout)."""

    outcome = RECALL_OUTCOME_CANCELLED


class RecallDeadlineExceeded(RecallInterrupted):
    """The retrieval's own deadline expired before it finished."""

    outcome = RECALL_OUTCOME_TIMED_OUT


class RecallCancellation:
    """Cooperative cancellation token + monotonic deadline for one retrieval.

    Providers that accept a ``cancel`` keyword on :meth:`MemoryProvider.prefetch`
    receive one of these from ``MemoryManager``. They should poll
    :meth:`should_stop` / :meth:`raise_if_stopped` at safe points and, for
    blocking work that can be interrupted from another thread (an SQLite
    statement, a socket), register the interrupter with
    :meth:`add_interrupt` so :meth:`cancel` can abort it immediately.

    Tokens compose: ``RecallCancellation(parent=token, timeout=6.0)`` is a
    child that stops when the parent is cancelled/expired OR its own tighter
    deadline passes; interrupters registered on the child fire for either.
    The manager never cancels a sibling request — every prefetch call gets
    its own token.
    """

    def __init__(
        self,
        *,
        timeout: Optional[float] = None,
        deadline: Optional[float] = None,
        parent: Optional["RecallCancellation"] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._reason = ""
        self._interrupts: List[Callable[[], None]] = []
        self._parent = parent
        own_deadline = deadline
        if timeout is not None:
            own_deadline = time.monotonic() + float(timeout)
        if parent is not None and parent.deadline is not None:
            own_deadline = (
                parent.deadline if own_deadline is None else min(own_deadline, parent.deadline)
            )
        self._deadline = own_deadline
        if parent is not None:
            parent.add_interrupt(self._on_parent_cancel)

    # -- state -----------------------------------------------------------

    @property
    def deadline(self) -> Optional[float]:
        """Monotonic deadline (``time.monotonic()`` scale) or ``None``."""
        return self._deadline

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set() or (
            self._parent is not None and self._parent.cancelled
        )

    @property
    def reason(self) -> str:
        if self._reason:
            return self._reason
        if self._parent is not None:
            return self._parent.reason
        return ""

    def remaining(self) -> Optional[float]:
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - time.monotonic())

    def expired(self) -> bool:
        return self._deadline is not None and time.monotonic() >= self._deadline

    def should_stop(self) -> bool:
        return self.cancelled or self.expired()

    def raise_if_stopped(self) -> None:
        """Raise the typed interruption matching the token state, if any."""
        if self.cancelled:
            raise RecallCancelled(self.reason or "retrieval cancelled")
        if self.expired():
            raise RecallDeadlineExceeded("retrieval deadline exceeded")

    def interruption(self) -> Optional[RecallInterrupted]:
        """The typed interruption for the current state, or ``None``."""
        try:
            self.raise_if_stopped()
        except RecallInterrupted as exc:
            return exc
        return None

    # -- cancellation ----------------------------------------------------

    def cancel(self, reason: str = "") -> None:
        """Flag the token and fire every registered interrupter once."""
        with self._lock:
            if self._cancelled.is_set():
                return
            self._reason = reason or "retrieval cancelled"
            self._cancelled.set()
            interrupts = list(self._interrupts)
        for fn in interrupts:
            try:
                fn()
            except Exception:
                logger.debug("recall interrupt callback failed", exc_info=True)

    def _on_parent_cancel(self) -> None:
        self.cancel(self._parent.reason if self._parent is not None else "")

    def add_interrupt(self, fn: Callable[[], None]) -> None:
        """Register ``fn`` to be called when the token is cancelled.

        If the token is already cancelled the callback fires immediately, so
        a late registration can never miss the signal.
        """
        with self._lock:
            already = self._cancelled.is_set()
            if not already:
                self._interrupts.append(fn)
        if already:
            try:
                fn()
            except Exception:
                logger.debug("recall interrupt callback failed", exc_info=True)

    def remove_interrupt(self, fn: Callable[[], None]) -> None:
        with self._lock:
            try:
                self._interrupts.remove(fn)
            except ValueError:
                pass

    def detach(self) -> None:
        """Unhook a child token from its parent once its work is finished."""
        if self._parent is not None:
            self._parent.remove_interrupt(self._on_parent_cancel)


# Prompts that carry no semantic signal — trivial acknowledgements, greetings,
# slash commands, empty input. Single source of truth shared by the core
# per-turn prefetch gate (agent/turn_context.py, run_agent.py) and provider-
# side classifiers (plugins/memory/honcho) so the two can never drift apart.
# The alternation is anchored and may only be followed by whitespace or
# punctuation, so words that merely START with a trivial word ("k8s", "yolo",
# "note", "hindsight") do NOT match, while trailing-punctuation variants
# ("hi!", "hey.", "thanks :)", "done???") do.
TRIVIAL_PROMPT_RE = re.compile(
    r'^(yes|no|ok|okay|sure|thanks|thank you|y|n|yep|nope|yeah|nah|'
    r'hi|hey|hello|yo|sup|'
    r'continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|lgtm|k)'
    r'[\s!?.:;,"' + "'" + r'~\u2018\u2019\u201c\u201d\u2014\u2013\u2026()\[\]{}<>*&^%$#@!+=`\u00a0]*$',
    re.IGNORECASE,
)


def is_trivial_prompt(text: Optional[str]) -> bool:
    """Return True if a user prompt is too trivial to warrant memory recall.

    Empty/whitespace-only input, slash commands, and bare greetings or
    acknowledgements (with optional trailing punctuation) all count as
    trivial. Callers use this to skip memory-provider prefetch/injection
    on turns that carry no semantic signal — saving a blocking network
    round-trip and preventing stale user-model context from derailing
    one-word replies.
    """
    if not text:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    if stripped.startswith("/"):
        return True
    return bool(TRIVIAL_PROMPT_RE.match(stripped))


class MemoryProvider(ABC):
    """Abstract base class for memory providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this provider (e.g. 'builtin', 'honcho', 'hindsight')."""

    # -- Core lifecycle (implement these) ------------------------------------

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this provider is configured, has credentials, and is ready.

        Called during agent init to decide whether to activate the provider.
        Should not make network calls — just check config and installed deps.
        """

    @abstractmethod
    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize for a session.

        Called once at agent startup. May create resources (banks, tables),
        establish connections, start background threads, etc.

        kwargs always include:
          - hermes_home (str): The active HERMES_HOME directory path. Use this
            for profile-scoped storage instead of hardcoding ``~/.hermes``.
          - platform (str): "cli", "telegram", "discord", "cron", etc.

        kwargs may also include:
          - agent_context (str): "primary", "subagent", "cron", or "flush".
            Providers should skip writes for non-primary contexts (cron system
            prompts would corrupt user representations).
          - agent_identity (str): Profile name (e.g. "coder"). Use for
            per-profile provider identity scoping.
          - agent_workspace (str): Shared workspace name (e.g. "hermes").
          - parent_session_id (str): For subagents, the parent's session_id.
          - user_id (str): Platform user identifier (gateway sessions).
          - user_id_alt (str): Optional alternate stable platform user identifier.
        """

    def unavailable_reason(self) -> str:
        """Actionable reason this provider reports unavailable, for the caller.

        ``is_available()`` gates initialization, so a provider that reports
        unavailable is never initialized — any diagnostic it would log from
        ``initialize()`` is unreachable. Return a short, user-facing hint here
        (e.g. which package to install) so the caller's "provider unavailable"
        warning can surface it. Empty string (the default) adds nothing.
        """
        return ""

    def system_prompt_block(self) -> str:
        """Return text to include in the system prompt.

        Called during system prompt assembly. Return empty string to skip.
        This is for STATIC provider info (instructions, status). Prefetched
        recall context is injected separately via prefetch().
        """
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant context for the upcoming turn.

        Called before each API call. Return formatted text to inject as
        context, or empty string if nothing relevant. Implementations
        should be fast — use background threads for the actual recall
        and return cached results here.

        session_id is provided for providers serving concurrent sessions
        (gateway group chats, cached agents). Providers that don't need
        per-session scoping can ignore it.

        ``query`` is already bounded by ``MemoryManager`` (see
        ``memory.prefetch_max_query_chars``); providers must still apply
        their own finite limits before any expensive expansion.

        Optional cancellation: a provider may declare an extra keyword-only
        ``cancel: Optional[RecallCancellation] = None`` parameter. The
        manager detects it by signature and passes a token carrying the
        prefetch deadline; on timeout the manager cancels the token instead
        of abandoning the worker. Cooperating providers should stop promptly
        and raise :class:`RecallCancelled` / :class:`RecallDeadlineExceeded`
        (typed outcomes) rather than returning ``""`` as if nothing matched.
        Providers without the keyword keep the legacy call shape.
        """
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Queue a background recall for the NEXT turn.

        Called after each turn completes. The result will be consumed
        by prefetch() on the next turn. Default is no-op — providers
        that do background prefetching should override this.
        """

    def recall_status(self) -> Optional[RecallStatus]:
        """Describe what the most recent :meth:`prefetch` injected, for the UI.

        Called by the agent right after prefetch, on the same (single) turn
        thread, so it can surface a deterministic "👁️ recalled N memories"
        status line that does not depend on the model choosing to mention it.

        Return ``None`` (the default) when this provider injected nothing this
        turn or does not want a visible indicator. Providers that override it
        must reflect only the LAST prefetch — never a stale prior count.
        """
        return None

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Persist a completed turn to the backend.

        Called after each turn. Should be non-blocking — queue for
        background processing if the backend has latency.

        ``messages`` is the OpenAI-style conversation message list as of the
        completed turn, including any assistant tool calls and tool results.
        Providers that do not need raw turn context can ignore it.
        """

    @abstractmethod
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return tool schemas this provider exposes.

        Each schema follows the OpenAI function calling format:
        {"name": "...", "description": "...", "parameters": {...}}

        Return empty list if this provider has no tools (context-only).
        """

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """Handle a tool call for one of this provider's tools.

        Must return a JSON string (the tool result).
        Only called for tool names returned by get_tool_schemas().
        """
        raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")

    def shutdown(self) -> None:
        """Clean shutdown — flush queues, close connections."""

    # -- Optional hooks (override to opt in) ---------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Called at the start of each turn with the user message.

        Use for turn-counting, scope management, periodic maintenance.

        kwargs may include: remaining_tokens, model, platform, tool_count.
        Providers use what they need; extras are ignored.
        """

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Called when a session ends (explicit exit or timeout).

        Use for end-of-session fact extraction, summarization, etc.
        messages is the full conversation history.

        NOT called after every turn — only at actual session boundaries
        (CLI exit, /reset, gateway session expiry).
        """

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        """Called when the agent switches session_id mid-process.

        Fires on ``/resume``, ``/branch``, ``/reset``, ``/new`` (CLI), the
        gateway equivalents, and context compression — any path that
        reassigns ``AIAgent.session_id`` without tearing the provider down.

        Providers that cache per-session state in ``initialize()``
        (``_session_id``, ``_document_id``, accumulated turn buffers,
        counters) should update or reset that state here so subsequent
        writes land in the correct session's record.

        Parameters
        ----------
        new_session_id:
            The session_id the agent just switched to.
        parent_session_id:
            The previous session_id, if meaningful — set for ``/branch``
            (fork lineage), context compression (continuation lineage),
            and ``/resume`` (the session we're leaving). Empty string
            when no lineage applies.
        reset:
            ``True`` when this is a genuinely new conversation, not a
            resumption of an existing one. Fired by ``/reset`` / ``/new``.
            Providers should flush accumulated per-session buffers
            (``_session_turns``, ``_turn_counter``, etc.) when this is
            set. ``False`` for ``/resume`` / ``/branch`` / compression
            where the logical conversation continues under the new id.
        rewound:
            ``True`` if session_id is unchanged but the transcript was
            truncated; providers caching per-turn document state should
            invalidate.

        Default is no-op for backward compatibility.
        """

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Called before context compression discards old messages.

        Use to extract insights from messages about to be compressed.
        messages is the list that will be summarized/discarded.

        Return text to include in the compression summary prompt so the
        compressor preserves provider-extracted insights. Return empty
        string for no contribution (backwards-compatible default).
        """
        return ""

    def on_delegation(self, task: str, result: str, *,
                      child_session_id: str = "", **kwargs) -> None:
        """Called on the PARENT agent when a subagent completes.

        The parent's memory provider gets the task+result pair as an
        observation of what was delegated and what came back. The subagent
        itself has no provider session (skip_memory=True).

        task: the delegation prompt
        result: the subagent's final response
        child_session_id: the subagent's session_id
        """

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """Return config fields this provider needs for setup.

        Used by 'hermes memory setup' to walk the user through configuration.
        Each field is a dict with:
          key:         config key name (e.g. 'api_key', 'mode')
          description: human-readable description
          secret:      True if this should go to .env (default: False)
          required:    True if required (default: False)
          default:     default value (optional)
          choices:     list of valid values (optional)
          type:        text, integer, number, or boolean (optional)
          minimum:     numeric lower bound for integer/number fields (optional)
          maximum:     numeric upper bound for integer/number fields (optional)
          step:        numeric input step for Dashboard rendering (optional)
          url:         URL where user can get this credential (optional)
          env_var:     explicit env var name for secrets (default: auto-generated)

        Return empty list if no config needed (e.g. local-only providers).
        """
        return []

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Write non-secret config to the provider's native location.

        Called by 'hermes memory setup' after collecting user inputs.
        ``values`` contains only non-secret fields (secrets go to .env).
        ``hermes_home`` is the active HERMES_HOME directory path.

        Providers with native config files (JSON, YAML) should override
        this to write to their expected location. Providers that use only
        env vars can leave the default (no-op).

        All new memory provider plugins MUST implement either:
        - save_config() for native config file formats, OR
        - use only env vars (in which case get_config_schema() fields
          should all have ``env_var`` set and this method stays no-op).
        """

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Called when the built-in memory tool writes an entry.

        action: 'add', 'replace', or 'remove'
        target: 'memory' or 'user'
        content: the entry content
        metadata: structured provenance for the write, when available. Common
          keys include ``write_origin``, ``execution_context``, ``session_id``,
          ``parent_session_id``, ``platform``, and ``tool_name``.

        Use to mirror built-in memory writes to your backend.
        """

    def backup_paths(self) -> List[str]:
        """Return extra on-disk paths this provider stores OUTSIDE HERMES_HOME.

        ``hermes backup`` only walks HERMES_HOME, so any provider state kept
        under ``~/.honcho``, ``~/.hindsight``, ``~/.openviking``, etc. is lost
        across a backup/import cycle unless it's declared here.

        Return a list of absolute path strings (files or directories). The
        backup command resolves each, captures the ones that exist and live
        under the user's home directory into a reserved ``_external/`` subtree
        of the archive, and ``hermes import`` restores them to their original
        locations. Paths outside the home directory are skipped for safety.

        MUST be callable without ``initialize()`` and without network — resolve
        from config/env only. Default returns an empty list (nothing external).
        """
        return []
