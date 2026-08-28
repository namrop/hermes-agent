"""Aux-chain exhaustion behavior in _smart_approve (2026-08-28 denial audit).

When the auxiliary approval provider AND all its fallbacks are exhausted, the
aux client re-raises the original error.  _smart_approve must:

  1. retry the whole chain ONCE after a short backoff for transient
     (rate-limit / connection) failures,
  2. degrade to manual approval ("escalate") — never approve, never deny —
     when the retry also fails or the failure is not retryable, and
  3. log the degradation at WARNING so an unattended denial storm is
     diagnosable from the journal.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

import tools.approval as approval_mod
from tools.approval import _aux_failure_is_retryable, _smart_approve


def _make_response(answer: str):
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = answer
    return mock_response


def _rate_limit_error():
    err = Exception("Error code: 429 - rate limit exceeded, too many requests")
    err.status_code = 429  # matches _is_rate_limit_error's detection contract
    return err


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Keep the retry backoff out of the test suite's runtime."""
    monkeypatch.setattr(approval_mod, "_SMART_APPROVE_RETRY_BACKOFF_SECONDS", 0.0)


class TestAuxFailureIsRetryable:
    def test_rate_limit_is_retryable(self):
        assert _aux_failure_is_retryable(_rate_limit_error())

    def test_connection_error_is_retryable(self):
        assert _aux_failure_is_retryable(Exception("Connection refused"))

    def test_auth_error_is_not_retryable(self):
        assert not _aux_failure_is_retryable(
            Exception("Error code: 401 - invalid api key")
        )

    def test_generic_error_is_not_retryable(self):
        assert not _aux_failure_is_retryable(ValueError("boom"))


class TestSmartApproveExhaustion:
    @patch("agent.auxiliary_client.call_llm")
    def test_rate_limit_retries_once_then_escalates(self, mock_call_llm, caplog):
        """Transient exhaustion: one retry, then manual degradation."""
        mock_call_llm.side_effect = [_rate_limit_error(), _rate_limit_error()]

        with caplog.at_level(logging.WARNING, logger="tools.approval"):
            verdict = _smart_approve("ls -la", "flagged command")

        assert verdict == "escalate"
        assert mock_call_llm.call_count == 2, "exactly one retry"
        assert any(
            "degrading to manual approval" in r.getMessage()
            for r in caplog.records
        ), "degradation must be WARNING-visible"

    @patch("agent.auxiliary_client.call_llm")
    def test_rate_limit_then_recovery_uses_retry_result(self, mock_call_llm):
        """A successful retry serves the verdict — no degradation."""
        mock_call_llm.side_effect = [_rate_limit_error(), _make_response("APPROVE")]

        verdict = _smart_approve("ls -la", "flagged command")

        assert verdict == "approve"
        assert mock_call_llm.call_count == 2

    @patch("agent.auxiliary_client.call_llm")
    def test_non_retryable_error_escalates_without_retry(self, mock_call_llm, caplog):
        mock_call_llm.side_effect = ValueError("schema exploded")

        with caplog.at_level(logging.WARNING, logger="tools.approval"):
            verdict = _smart_approve("ls -la", "flagged command")

        assert verdict == "escalate"
        assert mock_call_llm.call_count == 1, "non-transient errors must not retry"
        assert any(
            "degrading to manual approval" in r.getMessage()
            for r in caplog.records
        )

    @patch("agent.auxiliary_client.call_llm")
    def test_exhaustion_never_approves_or_denies(self, mock_call_llm):
        """Aux death must fall to a human, not decide on its own."""
        mock_call_llm.side_effect = _rate_limit_error()
        assert _smart_approve("rm -rf /", "recursive delete") == "escalate"
