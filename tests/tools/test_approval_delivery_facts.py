"""Delivery-facts closure for the approval hooks — 2026-08-31 scar, built 2026-09-10.

The scar's own words: "The closure is one kwarg: pass ``notification_ping:
bool`` through ``_await_gateway_decision`` into the ``post_approval_response``
fire site. The field is already there waiting."

This suite pins the three layers of that closure:

* **Gate layer** (``tools/approval.py``): ``_await_gateway_decision`` pockets
  a ``_delivery`` dict into ``approval_data`` before enqueueing; the gateway
  notify callback stamps first-hand facts into that same object; the facts
  are forwarded into ``post_approval_response`` as validated kwargs.
* **Ledger layer** (``plugins/observability/approval_ledger``):
  ``_escalation`` turns a first-hand ``notification_ping`` into an honest
  ``ping_dispatched`` (true/false/null — never inferred), refines
  ``notification_dispatched`` from ``notification_send``, and
  ``_outcome_class`` gains the ``never_notified`` bucket the scar asked for:
  a timeout on a human-tier row whose prompt demonstrably carried no mention.
* **Unknown stays unknown**: unstamped surfaces keep byte-identical
  escalation rows and the old four outcome classes — the closure adds
  information, it does not reinterpret history.

Why pocket instead of a literal signature kwarg: ``notify_cb`` receives the
approval_data dict by reference and already mutates nothing; sharing one
small dict is the same coupling with no signature change at any of the four
``_await_gateway_decision`` call sites, and the pocket is invisible to every
consumer that does not know the key.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

import tools.approval as approval_module
from tools.approval import (
    _await_gateway_decision,
    _delivery_hook_kwargs,
    _new_delivery_facts,
    set_current_session_key,
)


# ---------------------------------------------------------------------------
# Unit layer — the pocket and the extractor
# ---------------------------------------------------------------------------


class TestDeliveryPocket:
    def test_pocket_created_with_null_facts(self):
        data = {"command": "rm -rf /tmp/x"}
        facts = _new_delivery_facts(data)
        assert data["_delivery"] is facts
        assert facts == {"notification_ping": None, "notification_send": None}

    def test_pocket_is_shared_through_entry_shallow_copy(self):
        # _ApprovalEntry shallow-copies approval_data; the pocket object must
        # survive the copy so gateway-side stamps are visible to the gate.
        data = {}
        facts = _new_delivery_facts(data)
        entry = approval_module._ApprovalEntry(dict(data))
        assert entry.data["_delivery"] is facts

    def test_extractor_validates_known_values_only(self):
        data = {}
        facts = _new_delivery_facts(data)
        assert _delivery_hook_kwargs(data) == {}
        facts["notification_ping"] = True
        facts["notification_send"] = "sent"
        assert _delivery_hook_kwargs(data) == {
            "notification_ping": True,
            "notification_send": "sent",
        }

    def test_extractor_rejects_garbage(self):
        data = {"_delivery": {"notification_ping": "yes", "notification_send": 7}}
        assert _delivery_hook_kwargs(data) == {}
        data["_delivery"] = "not-a-dict"
        assert _delivery_hook_kwargs(data) == {}
        assert _delivery_hook_kwargs({}) == {}

    def test_extractor_admits_false_and_none(self):
        data = {}
        facts = _new_delivery_facts(data)
        facts["notification_ping"] = False
        facts["notification_send"] = "failed"
        assert _delivery_hook_kwargs(data) == {
            "notification_ping": False,
            "notification_send": "failed",
        }


# ---------------------------------------------------------------------------
# Gate layer — the wait loop forwards stamped facts
# ---------------------------------------------------------------------------


class TestAwaitForwardsDeliveryFacts:
    def _run(self, approval_data, notify_cb, captured, choice="deny"):
        session_key = "test:delivery:fwd"
        token = set_current_session_key(session_key)
        try:
            with patch(
                "hermes_cli.plugins.invoke_hook",
                side_effect=lambda name, **kw: captured.append((name, kw)) or [],
            ):
                # Resolve from another thread once the request is queued.
                def resolver():
                    for _ in range(200):
                        with approval_module._lock:
                            queue = approval_module._gateway_queues.get(session_key, [])
                            if queue:
                                queue[0].result = choice
                                queue[0].event.set()
                                return
                        time.sleep(0.01)

                threading.Thread(target=resolver, daemon=True).start()
                return _await_gateway_decision(
                    session_key, notify_cb, approval_data, surface="gateway"
                )
        finally:
            approval_module._approval_session_key.reset(token)

    def test_stamped_facts_reach_post_hook(self):
        captured = []

        def notify_cb(approval_data):
            # The gateway layer stamps during the send — simulate exactly that.
            approval_data["_delivery"]["notification_ping"] = True
            approval_data["_delivery"]["notification_send"] = "sent"

        decision = self._run({"command": "echo hi"}, notify_cb, captured)
        assert decision["resolved"] is True
        post = next(kw for name, kw in captured if name == "post_approval_response")
        assert post["notification_ping"] is True
        assert post["notification_send"] == "sent"

    def test_unstamped_sends_no_kwargs(self):
        captured = []

        def notify_cb(approval_data):
            pass  # older surface / notify ran but nothing stamped

        self._run({"command": "echo hi"}, notify_cb, captured)
        post = next(kw for name, kw in captured if name == "post_approval_response")
        assert "notification_ping" not in post
        assert "notification_send" not in post


# ---------------------------------------------------------------------------
# Ledger layer — honest escalation + the never_notified bucket
# ---------------------------------------------------------------------------


class TestLedgerEscalation:
    def test_first_hand_ping_true(self):
        esc = ledger()._escalation(
            "gateway", "timeout", notification_ping=True, notification_send="sent"
        )
        assert esc["ping_dispatched"] is True
        assert esc["notification_dispatched"] is True

    def test_first_hand_ping_false_yields_never_notified(self):
        esc = ledger()._escalation(
            "gateway", "timeout", notification_ping=False, notification_send="sent"
        )
        assert esc["ping_dispatched"] is False
        assert ledger()._outcome_class("gateway", "timed_out", esc) == "never_notified"

    def test_unknown_ping_timeout_stays_human_never_answered(self):
        esc = ledger()._escalation("gateway", "timeout")
        assert esc["ping_dispatched"] is None
        assert (
            ledger()._outcome_class("gateway", "timed_out", esc)
            == "human_never_answered"
        )

    def test_pinged_timeout_stays_human_never_answered(self):
        # Notified and still no answer — never_notified would be a lie.
        esc = ledger()._escalation("gateway", "timeout", notification_ping=True)
        assert (
            ledger()._outcome_class("gateway", "timed_out", esc)
            == "human_never_answered"
        )

    def test_send_failed_refines_dispatched(self):
        esc = ledger()._escalation(
            "gateway", "timeout", notification_ping=False, notification_send="failed"
        )
        assert esc["notification_dispatched"] is False

    def test_send_ambiguous_does_not_overwrite_fallback(self):
        # ambiguous is not a failure claim; keep the choice-based inference.
        esc = ledger()._escalation(
            "gateway", "timeout", notification_ping=True, notification_send="ambiguous"
        )
        assert esc["notification_dispatched"] is True  # gateway, not notify_failed

    def test_smart_rows_unchanged(self):
        esc = ledger()._escalation("smart", "smart_deny", notification_ping=None)
        assert esc["human_tier_reached"] is False
        assert esc["ping_dispatched"] is None
        assert (
            ledger()._outcome_class("smart", "denied", esc) == "resolved_by_aux"
        )

    def test_legacy_call_shape_unchanged(self):
        # Two-arg calls (request rows) must behave exactly as before.
        esc = ledger()._escalation("gateway", None)
        assert esc == {
            "human_tier_reached": True,
            "notification_channel": "gateway_card",
            "notification_dispatched": True,
            "ping_dispatched": None,
        }


_ledger_module = None


def ledger():
    global _ledger_module
    if _ledger_module is None:
        import importlib.util
        import pathlib

        spec = importlib.util.spec_from_file_location(
            "approval_ledger_under_test",
            pathlib.Path(__file__).resolve().parents[2]
            / "plugins"
            / "observability"
            / "approval_ledger"
            / "__init__.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _ledger_module = module
    return _ledger_module
