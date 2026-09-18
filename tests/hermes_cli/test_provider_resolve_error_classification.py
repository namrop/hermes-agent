"""One shared classifier for resolve-time fallback eligibility.

``cron/scheduler.py`` had the only implementation of "is this provider-resolution
failure worth walking ``fallback_providers`` for?". Agent init had none, so fresh
gateway turns hard-failed on a benched credential pool while cron jobs in the
same minute walked to a healthy rung (Sol, 2026-09-18). The predicate now lives
in ``hermes_cli.fallback_config`` and the scheduler delegates to it — these tests
pin both the behavior and the delegation.
"""

import errno
import socket

import pytest

from hermes_cli.fallback_config import (
    classify_provider_resolve_error,
    is_transient_provider_resolve_error,
)


def test_auth_error_classifies_as_auth():
    from hermes_cli.auth import AuthError

    assert (
        classify_provider_resolve_error(
            AuthError("credential pool: no available entries (all exhausted or empty)")
        )
        == "auth"
    )


def test_gaierror_classifies_as_transient():
    exc = socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")
    assert is_transient_provider_resolve_error(exc) is True
    assert classify_provider_resolve_error(exc) == "transient network"


@pytest.mark.parametrize(
    "err_no",
    [
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.ETIMEDOUT,
    ],
)
def test_transient_oserror_errnos(err_no):
    assert is_transient_provider_resolve_error(OSError(err_no, "boom")) is True


def test_dns_text_in_wrapped_runtime_error_is_transient():
    inner = OSError(8, "nodename nor servname provided")
    outer = RuntimeError("provider resolution failed")
    outer.__cause__ = inner
    assert is_transient_provider_resolve_error(outer) is True


def test_plain_value_error_is_not_eligible():
    """The load-bearing negative: a resolver bug must NOT trigger a reroute."""
    exc = ValueError("resolver logic error")
    assert is_transient_provider_resolve_error(exc) is False
    assert classify_provider_resolve_error(exc) is None


def test_permission_error_is_not_eligible():
    assert classify_provider_resolve_error(PermissionError("nope")) is None


def test_cause_cycle_terminates():
    """A self-referential cause chain must not spin forever."""
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert is_transient_provider_resolve_error(a) is False


def test_cron_scheduler_delegates_to_the_shared_predicate():
    """Exactly one implementation: the scheduler alias must forward to it."""
    from cron import scheduler

    exc = socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")
    assert scheduler._is_transient_provider_resolve_error(exc) is True
    assert scheduler._is_transient_provider_resolve_error(ValueError("x")) is False

    calls = []
    real = is_transient_provider_resolve_error

    import hermes_cli.fallback_config as fc

    def spy(e):
        calls.append(e)
        return real(e)

    fc.is_transient_provider_resolve_error = spy
    try:
        scheduler._is_transient_provider_resolve_error(exc)
    finally:
        fc.is_transient_provider_resolve_error = real

    assert calls == [exc], "scheduler must not carry its own copy of the predicate"


def test_unknown_provider_name_is_a_config_error_not_a_fallback_trigger():
    """A typo in config.yaml must surface, not hide behind a working rung."""
    from hermes_cli.auth import AuthError

    exc = AuthError("Unknown provider 'opencodego'.", code="invalid_provider")
    assert classify_provider_resolve_error(exc) is None


def test_no_provider_configured_is_a_config_error():
    from hermes_cli.auth import AuthError

    exc = AuthError("No provider configured.", code="no_provider_configured")
    assert classify_provider_resolve_error(exc) is None


def test_credential_state_auth_codes_remain_eligible():
    """Missing/expired/quota-exhausted credentials DO deserve a fallback."""
    from hermes_cli.auth import AuthError

    for code in (
        "codex_auth_missing",
        "refresh_failed",
        "invalid_token",
        "not_logged_in",
        None,
    ):
        exc = AuthError("credential unusable", code=code)
        assert classify_provider_resolve_error(exc) == "auth", code
