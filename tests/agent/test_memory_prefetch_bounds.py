"""MemoryManager: bounded recall intent, explicit memory queries, real
cancellation on timeout, and typed visible recall outcomes.

Regression for the 2026-09-17/18 gateway freeze: ``prefetch_all`` forwarded a
multi-megabyte cron packet verbatim to the external provider and, on timeout,
merely stopped waiting (``thread.join(8)``) while the worker kept running and
holding shared resources. The manager now:

* bounds the query BEFORE dispatch (``prefetch_max_query_chars``);
* accepts an explicit ``memory_query`` (recall intent) separate from the
  model-facing message, where ``""`` means "skip automatic recall";
* hands cancellation-aware providers a ``RecallCancellation`` token and
  cancels it on timeout, observing worker completion instead of abandoning it;
* keeps the legacy ``prefetch(query, *, session_id)`` signature working;
* records a typed per-provider outcome and renders skip/timeout visibly via
  ``describe_recall`` while a successful no-hit stays silent.
"""
from __future__ import annotations

import inspect
import threading
import time
from typing import Optional

import pytest

from agent.memory_manager import (
    MemoryManager,
    PrefetchOutcome,
    prepare_recall_query,
)
from agent.memory_provider import (
    MemoryProvider,
    RecallCancellation,
    RecallCancelled,
    RecallDeadlineExceeded,
    RecallStatus,
)


class _LegacyProvider(MemoryProvider):
    """Old-style provider: no ``cancel`` keyword on prefetch."""

    def __init__(self, name="legacy", result="legacy memory"):
        self._name = name
        self._result = result
        self.calls: list[tuple[str, dict]] = []
        self._status: Optional[RecallStatus] = None

    @property
    def name(self):
        return self._name

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        pass

    def get_tool_schemas(self):
        return []

    def prefetch(self, query, *, session_id=""):
        self.calls.append((query, {"session_id": session_id}))
        return self._result

    def recall_status(self):
        return self._status


class _CancelAwareProvider(_LegacyProvider):
    """Provider that cooperates with the cancellation token."""

    def __init__(self, name="aware", result="aware memory", *, block=False, raise_exc=None):
        super().__init__(name=name, result=result)
        self.block = block
        self.raise_exc = raise_exc
        self.release = threading.Event()
        self.cancel_seen: list[RecallCancellation] = []
        self.finished = threading.Event()

    def prefetch(self, query, *, session_id="", cancel=None):
        self.calls.append((query, {"session_id": session_id, "cancel": cancel}))
        try:
            if self.raise_exc is not None:
                raise self.raise_exc
            if self.block:
                while not self.release.is_set():
                    if cancel is not None and cancel.should_stop():
                        self.cancel_seen.append(cancel)
                        cancel.raise_if_stopped()
                    time.sleep(0.005)
            return self._result
        finally:
            self.finished.set()


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------------
# Pure preparation
# ---------------------------------------------------------------------------


def test_prepare_recall_query_keeps_short_text_and_bounds_long_text():
    short = "what did we decide about the deploy pipeline?"
    assert prepare_recall_query(short, max_chars=4000) == (short, False)

    packet = "## Script Output\n" + " ".join(f"w{i}" for i in range(200_000)) + "\nReflect."
    started = time.monotonic()
    query, bounded = prepare_recall_query(packet, max_chars=300)
    assert time.monotonic() - started < 1.0
    assert bounded is True
    assert 0 < len(query) <= 300
    assert not query.endswith(" ")


# ---------------------------------------------------------------------------
# Bounding and explicit intent
# ---------------------------------------------------------------------------


def test_prefetch_query_is_bounded_before_dispatch():
    mgr = MemoryManager(prefetch_max_query_chars=200)
    provider = _LegacyProvider()
    mgr.add_provider(provider)
    packet = "## Script Output\n" + " ".join(f"w{i}" for i in range(50_000))

    assert mgr.prefetch_all(packet) == "legacy memory"
    query, _ = provider.calls[0]
    assert len(query) <= 200
    outcome = mgr.prefetch_outcomes["legacy"]
    assert isinstance(outcome, PrefetchOutcome)
    assert outcome.outcome == "recalled"
    assert outcome.bounded is True
    assert outcome.query_chars <= 200


def test_explicit_memory_query_overrides_the_message():
    mgr = MemoryManager()
    provider = _LegacyProvider()
    mgr.add_provider(provider)
    packet = "## Script Output\n" + "x" * 100_000

    assert mgr.prefetch_all(packet, memory_query="deploy rollback status") == "legacy memory"
    assert provider.calls[0][0] == "deploy rollback status"


def test_empty_memory_query_skips_with_typed_visible_status():
    mgr = MemoryManager()
    provider = _LegacyProvider()
    mgr.add_provider(provider)

    assert mgr.prefetch_all("a self-contained packet", memory_query="") == ""
    assert provider.calls == []
    outcome = mgr.prefetch_outcomes["legacy"]
    assert outcome.outcome == "skipped"
    assert "skipped" in mgr.describe_recall()


# ---------------------------------------------------------------------------
# Timeout → real cancellation
# ---------------------------------------------------------------------------


def test_timeout_cancels_cancel_aware_provider_and_reports_status():
    mgr = MemoryManager(external_prefetch_timeout=0.2)
    provider = _CancelAwareProvider(block=True)
    mgr.add_provider(provider)

    started = time.monotonic()
    assert mgr.prefetch_all("what happened with the deploy?") == ""
    assert time.monotonic() - started < 3.0

    assert _wait_until(provider.finished.is_set, timeout=3.0), "worker kept running after cancel"
    assert provider.cancel_seen, "provider never observed the cancellation token"
    assert _wait_until(lambda: mgr.prefetch_diagnostics()["active_workers"] == 0, timeout=3.0)
    outcome = mgr.prefetch_outcomes["aware"]
    assert outcome.outcome == "timed_out"
    assert "timed out" in mgr.describe_recall()


def test_legacy_provider_keeps_old_prefetch_signature():
    mgr = MemoryManager()
    provider = _LegacyProvider()
    mgr.add_provider(provider)
    sig = inspect.signature(provider.prefetch)
    assert "cancel" not in sig.parameters

    assert mgr.prefetch_all("ordinary question", session_id="s1") == "legacy memory"
    assert provider.calls == [("ordinary question", {"session_id": "s1"})]


def test_cancel_aware_provider_receives_a_token_with_deadline():
    mgr = MemoryManager(external_prefetch_timeout=5.0)
    provider = _CancelAwareProvider()
    mgr.add_provider(provider)

    assert mgr.prefetch_all("ordinary question") == "aware memory"
    token = provider.calls[0][1]["cancel"]
    assert isinstance(token, RecallCancellation)
    assert token.deadline is not None
    assert not token.cancelled


def test_repeated_timeouts_do_not_accumulate_worker_threads():
    mgr = MemoryManager(external_prefetch_timeout=0.1)
    provider = _CancelAwareProvider(block=True)
    mgr.add_provider(provider)
    before = threading.active_count()

    for i in range(6):
        provider.finished.clear()
        assert mgr.prefetch_all(f"question {i}") == ""
        assert _wait_until(lambda: mgr.prefetch_diagnostics()["active_workers"] == 0, timeout=3.0)

    assert len(provider.calls) == 6, "a timed-out turn must not silently skip later turns once cancelled"
    assert threading.active_count() <= before + 1


def test_typed_provider_deadline_exception_is_recorded_not_raised():
    mgr = MemoryManager()
    provider = _CancelAwareProvider(raise_exc=RecallDeadlineExceeded("retrieval deadline 6.0s exceeded"))
    mgr.add_provider(provider)

    assert mgr.prefetch_all("ordinary question") == ""
    outcome = mgr.prefetch_outcomes["aware"]
    assert outcome.outcome == "timed_out"
    assert "timed out" in mgr.describe_recall()


def test_timed_out_indicator_keeps_cause_without_invented_manager_budget():
    mgr = MemoryManager(external_prefetch_timeout=8.0)

    rendered = mgr._render_outcome(
        "🧠", "Holo", "timed_out", "retrieval deadline 0.05s exceeded"
    )

    assert rendered == "🧠 Holo — recall timed out (retrieval deadline 0.05s exceeded)"
    assert "8.0s" not in rendered


def test_provider_cancelled_exception_is_recorded_as_cancelled():
    mgr = MemoryManager()
    provider = _CancelAwareProvider(raise_exc=RecallCancelled("caller cancelled"))
    mgr.add_provider(provider)

    assert mgr.prefetch_all("ordinary question") == ""
    assert mgr.prefetch_outcomes["aware"].outcome == "cancelled"


# ---------------------------------------------------------------------------
# Typed recall outcomes through RecallStatus
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (RecallStatus("Holo", 3), "🧠 Holo — recalled 3 memories"),
        (RecallStatus("Holo", 0, outcome="no_hits"), ""),
        (RecallStatus("Holo", 0, outcome="timed_out", detail="retrieval deadline 6.0s"), "timed out"),
        (RecallStatus("Holo", 0, outcome="skipped", detail="no recall intent"), "skipped"),
    ],
)
def test_describe_recall_renders_typed_outcomes(status, expected):
    mgr = MemoryManager()
    provider = _LegacyProvider(result="")
    provider._status = status
    mgr.add_provider(provider)
    mgr.prefetch_all("ordinary question")
    rendered = mgr.describe_recall()
    if expected == "":
        assert rendered == ""
    else:
        assert expected in rendered


def test_recall_status_defaults_stay_backwards_compatible():
    status = RecallStatus(provider_label="Hindsight", count=2, glyph="👁️")
    assert status.outcome == "recalled"
    assert status.detail == ""


def test_prefetch_diagnostics_never_include_query_text():
    mgr = MemoryManager()
    provider = _LegacyProvider()
    mgr.add_provider(provider)
    mgr.prefetch_all("SECRET-QUERY-TEXT about the deploy")
    diag = mgr.prefetch_diagnostics()
    assert "SECRET-QUERY-TEXT" not in repr(diag)
    assert diag["outcomes"]["legacy"]["outcome"] == "recalled"
    assert diag["outcomes"]["legacy"]["query_chars"] == len("SECRET-QUERY-TEXT about the deploy")
