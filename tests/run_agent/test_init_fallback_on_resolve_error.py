"""Init-time provider resolution that RAISES must walk ``fallback_providers``.

Companion to ``test_init_fallback_on_exhausted_pool.py`` (#17929), which only
covers the case where ``resolve_provider_client`` *returns* ``(None, None)``.

Production incident (Sol, 2026-09-18): the hourly ``ai-quota-bench.timer`` marked
the default provider's sole credential-pool entry ``exhausted`` (pre-emptive
weekly-quota bench). Every session created AFTER that bench died at provider
resolution:

    agent.credential_pool: credential pool: no available entries (all exhausted or empty)
    gateway.run: response ready: ... time=0.2s api_calls=0 response=122 chars

The 122-char body was the canned "Provider authentication failed" reply. No
``OpenAI client created`` line was emitted at all — resolution raised, so the
conversation-loop fallback machinery (which lives downstream of client creation)
never engaged, and the init-time walk above never fired because it keys on a
``None`` return rather than a raised error.

Two clear rungs (``zai``, ``custom:meridian-yugen``) were sitting unbenched in
``fallback_providers`` the entire time. Already-live sessions walked to them
fine; cron jobs walked to them fine (``cron/scheduler.py`` has its own
resolve-time walk). Only fresh gateway turns hard-failed.
"""

import errno
import socket

import pytest
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _make_tool_defs():
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "search",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def _mock_client(api_key="fb-key-1234567890", base_url="https://fb.example.com/v1"):
    c = MagicMock()
    c.api_key = api_key
    c.base_url = base_url
    c._default_headers = None
    return c


def _auth_error(msg="credential pool: no available entries (all exhausted or empty)"):
    from hermes_cli.auth import AuthError

    exc = AuthError(msg)
    # resolve_runtime_provider attaches the offending provider when it can.
    exc.provider = "opencode-go"
    return exc


def _build(**overrides):
    kwargs = dict(
        provider="opencode-go",
        model="glm-5.3",
        api_key=None,
        base_url=None,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    kwargs.update(overrides)
    return AIAgent(**kwargs)


def _patched(fake_resolve):
    """Common patch stack: only the provider router varies per test."""
    return (
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=fake_resolve),
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    )


def _run(fake_resolve, **overrides):
    p1, p2, p3, p4 = _patched(fake_resolve)
    with p1, p2, p3, p4:
        return _build(**overrides)


# --------------------------------------------------------------------------
# The incident itself
# --------------------------------------------------------------------------


def test_benched_primary_pool_raising_auth_error_walks_the_chain():
    """The reported failure: benched pool raises AuthError on a fresh session."""
    fb = _mock_client(base_url="http://127.0.0.1:3457")

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        if provider == "opencode-go":
            raise _auth_error()
        if provider == "custom:meridian-yugen":
            return fb, "claude-sonnet-5"
        return None, None

    agent = _run(
        fake_resolve,
        fallback_model=[
            {"provider": "custom:meridian-yugen", "model": "claude-sonnet-5"},
        ],
    )

    assert agent.provider == "custom:meridian-yugen"
    assert agent.model == "claude-sonnet-5"
    assert agent._fallback_activated is True


def test_transient_network_resolve_error_walks_the_chain():
    """A DNS/connect blip during OAuth refresh is eligible, same as cron."""
    fb = _mock_client()

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        if provider == "opencode-go":
            raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")
        if provider == "zai":
            return fb, "glm-5.3-flash"
        return None, None

    agent = _run(
        fake_resolve,
        fallback_model=[{"provider": "zai", "model": "glm-5.3-flash"}],
    )

    assert agent.provider == "zai"
    assert agent.model == "glm-5.3-flash"
    assert agent._fallback_activated is True


def test_oserror_connection_refused_walks_the_chain():
    fb = _mock_client()

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        if provider == "opencode-go":
            raise OSError(errno.ECONNREFUSED, "Connection refused")
        if provider == "zai":
            return fb, "glm-5.3-flash"
        return None, None

    agent = _run(
        fake_resolve,
        fallback_model=[{"provider": "zai", "model": "glm-5.3-flash"}],
    )
    assert agent.provider == "zai"


# --------------------------------------------------------------------------
# Boundaries — what must NOT change
# --------------------------------------------------------------------------


def test_non_auth_non_transient_resolve_error_is_not_swallowed():
    """A genuine bug during resolution must surface, not silently reroute."""
    fb = _mock_client()
    sentinel = "resolver exploded in a way we do not understand"

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        if provider == "opencode-go":
            raise ValueError(sentinel)
        return fb, "glm-5.3-flash"

    p1, p2, p3, p4 = _patched(fake_resolve)
    with p1, p2, p3, p4:
        with pytest.raises(Exception) as excinfo:
            _build(fallback_model=[{"provider": "zai", "model": "glm-5.3-flash"}])

    assert sentinel in str(excinfo.value)


def test_all_fallbacks_failing_still_raises():
    """No rung available → the caller still gets a hard error, as today."""

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        raise _auth_error()

    p1, p2, p3, p4 = _patched(fake_resolve)
    with p1, p2, p3, p4:
        with pytest.raises(Exception):
            _build(
                fallback_model=[
                    {"provider": "kimi-coding", "model": "k3"},
                    {"provider": "zai", "model": "glm-5.3-flash"},
                ]
            )


def test_no_fallback_configured_still_raises():
    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        raise _auth_error()

    p1, p2, p3, p4 = _patched(fake_resolve)
    with p1, p2, p3, p4:
        with pytest.raises(Exception):
            _build(fallback_model=None)


# --------------------------------------------------------------------------
# Walk semantics
# --------------------------------------------------------------------------


def test_chain_is_walked_in_order_skipping_failing_rungs():
    """Entry 1 benched, entry 2 clear → entry 2 wins, and order is respected."""
    attempted = []
    fb = _mock_client()

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        attempted.append(provider)
        if provider == "opencode-go":
            raise _auth_error()
        if provider == "kimi-coding":
            raise _auth_error("pre-emptive quota bench at 100.0% of weekly week")
        if provider == "custom:meridian-yugen":
            return fb, "claude-sonnet-5"
        return None, None

    agent = _run(
        fake_resolve,
        fallback_model=[
            {"provider": "kimi-coding", "model": "k3"},
            {"provider": "custom:meridian-yugen", "model": "claude-sonnet-5"},
            {"provider": "zai", "model": "glm-5.3-flash"},
        ],
    )

    assert agent.provider == "custom:meridian-yugen"
    assert attempted.index("kimi-coding") < attempted.index("custom:meridian-yugen")
    # zai sits below the winning rung and must never be consulted.
    assert "zai" not in attempted


def test_chain_entry_matching_the_failed_primary_is_skipped():
    """Retrying the identical benched backend is pointless (cron logs this skip)."""
    attempted = []
    fb = _mock_client()

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        attempted.append(provider)
        if provider == "opencode-go":
            raise _auth_error()
        if provider == "zai":
            return fb, "glm-5.3-flash"
        return None, None

    agent = _run(
        fake_resolve,
        fallback_model=[
            # Same provider as the failed primary — must be skipped, not retried.
            {"provider": "opencode-go", "model": "glm-5.3-air"},
            {"provider": "zai", "model": "glm-5.3-flash"},
        ],
    )

    assert agent.provider == "zai"
    assert attempted.count("opencode-go") == 1, (
        "the benched primary must not be re-attempted as its own fallback rung"
    )


def test_provider_and_model_stay_atomic():
    """Never keep the primary's model against a substituted provider."""
    fb = _mock_client()

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        if provider == "opencode-go":
            raise _auth_error()
        if provider == "zai":
            return fb, "glm-5.3-flash"
        return None, None

    agent = _run(
        fake_resolve,
        fallback_model=[{"provider": "zai", "model": "glm-5.3-flash"}],
    )

    assert agent.provider == "zai"
    assert agent.model == "glm-5.3-flash"
    assert agent.model != "glm-5.3", "primary model must not survive the provider swap"


def test_incomplete_chain_entries_are_ignored():
    """Entries missing provider or model are skipped without exploding."""
    fb = _mock_client()

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        if provider == "opencode-go":
            raise _auth_error()
        if provider == "zai":
            return fb, "glm-5.3-flash"
        return None, None

    agent = _run(
        fake_resolve,
        fallback_model=[
            {"provider": "", "model": "x"},
            {"provider": "custom:omlx"},
            {"model": "orphan-model"},
            {"provider": "zai", "model": "glm-5.3-flash"},
        ],
    )
    assert agent.provider == "zai"


# --------------------------------------------------------------------------
# The walk must be visible
# --------------------------------------------------------------------------


def test_walk_records_a_user_visible_fallback_hop():
    """A silent substitution is the failure mode we are replacing, not keeping."""
    fb = _mock_client(base_url="http://127.0.0.1:3457")

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        if provider == "opencode-go":
            raise _auth_error()
        if provider == "custom:meridian-yugen":
            return fb, "claude-sonnet-5"
        return None, None

    agent = _run(
        fake_resolve,
        fallback_model=[{"provider": "custom:meridian-yugen", "model": "claude-sonnet-5"}],
    )

    hops = getattr(agent, "_pending_fallback_hops", None)
    assert hops, "init-time fallback must record a hop so the user sees the switch"
    kind, old_model, old_provider, new_model, new_provider = hops[-1]
    assert kind == "hop"
    assert (old_model, old_provider) == ("glm-5.3", "opencode-go")
    assert (new_model, new_provider) == ("claude-sonnet-5", "custom:meridian-yugen")

    notice = type(agent)._render_fallback_notice(hops)
    assert "glm-5.3 via opencode-go" in notice
    assert "claude-sonnet-5 via custom:meridian-yugen" in notice
