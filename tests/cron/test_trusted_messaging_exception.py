"""Behavior contract for the per-job trusted-messaging exception.

``_resolve_cron_disabled_toolsets`` keeps cron-spawned agents non-recursive
and non-interactive. The ported divergence from primary ``25b75e2663`` adds
one narrow escape hatch: a job persisted with
``cron_toolset_exceptions: ["messaging"]`` (a trusted local notifier job)
may opt back into ``messaging`` so it can post bounded notifications into
existing channels/threads.

Invariants under test:

  - no exception field, or an empty/None one: byte-exact current behavior.
  - the allowlist is exactly ``{"messaging"}`` — ``clarify`` (would block a
    non-interactive run) and ``cronjob`` (loop prevention; re-enable stays
    with the owner-level ``cron.allow_agent_scheduling`` config gate) can
    never be re-enabled from persisted job data.
  - the exception only lifts the *cron-context* automatic denial: it never
    widens past the user-level ``agent.disabled_toolsets`` denylist, because
    owner config outranks job data (#25752).
"""

import pytest

from cron.scheduler import _resolve_cron_disabled_toolsets

BASE_DENYLIST = ["cronjob", "messaging", "clarify"]
GATE_ON_DENYLIST = ["messaging", "clarify"]


class TestNoExceptionParity:
    """Absent/empty exception field must be byte-exact with current behavior."""

    @pytest.mark.parametrize("job", [None, {}, {"cron_toolset_exceptions": None}])
    def test_no_field_matches_base(self, job):
        assert _resolve_cron_disabled_toolsets({}, job) == BASE_DENYLIST

    def test_empty_string_is_noop(self):
        assert _resolve_cron_disabled_toolsets({}, {"cron_toolset_exceptions": ""}) == BASE_DENYLIST

    def test_empty_list_is_noop(self):
        assert _resolve_cron_disabled_toolsets({}, {"cron_toolset_exceptions": []}) == BASE_DENYLIST

    def test_gate_on_without_exception(self):
        cfg = {"cron": {"allow_agent_scheduling": True}}
        assert _resolve_cron_disabled_toolsets(cfg, {}) == GATE_ON_DENYLIST


class TestMessagingException:
    def test_string_form_lifts_messaging(self):
        job = {"cron_toolset_exceptions": "messaging"}
        assert _resolve_cron_disabled_toolsets({}, job) == ["cronjob", "clarify"]

    def test_list_form_lifts_messaging(self):
        job = {"cron_toolset_exceptions": ["messaging"]}
        assert _resolve_cron_disabled_toolsets({}, job) == ["cronjob", "clarify"]

    def test_tuple_form_lifts_messaging(self):
        job = {"cron_toolset_exceptions": ("messaging",)}
        assert _resolve_cron_disabled_toolsets({}, job) == ["cronjob", "clarify"]

    def test_exception_with_gate_on(self):
        cfg = {"cron": {"allow_agent_scheduling": True}}
        job = {"cron_toolset_exceptions": ["messaging"]}
        assert _resolve_cron_disabled_toolsets(cfg, job) == ["clarify"]


class TestAllowlistNarrowness:
    def test_clarify_cannot_be_reenabled(self):
        job = {"cron_toolset_exceptions": ["clarify"]}
        assert _resolve_cron_disabled_toolsets({}, job) == BASE_DENYLIST

    def test_cronjob_cannot_be_reenabled_from_job_data(self):
        job = {"cron_toolset_exceptions": ["cronjob"]}
        assert _resolve_cron_disabled_toolsets({}, job) == BASE_DENYLIST

    @pytest.mark.parametrize(
        "requested",
        [
            ["messaging", "clarify"],
            ["messaging", "clarify", "cronjob"],
            ["messaging", "terminal", "cronjob"],
        ],
    )
    def test_mixed_requests_lift_only_messaging(self, requested):
        job = {"cron_toolset_exceptions": requested}
        assert _resolve_cron_disabled_toolsets({}, job) == ["cronjob", "clarify"]

    def test_unknown_names_ignored(self):
        job = {"cron_toolset_exceptions": ["terminal", "file", "web"]}
        assert _resolve_cron_disabled_toolsets({}, job) == BASE_DENYLIST


class TestUserDenylistPrecedence:
    def test_user_denylist_beats_job_exception(self):
        cfg = {"agent": {"disabled_toolsets": ["messaging"]}}
        job = {"cron_toolset_exceptions": ["messaging"]}
        assert _resolve_cron_disabled_toolsets(cfg, job) == BASE_DENYLIST

    def test_user_denylist_other_toolsets_still_layered(self):
        cfg = {"agent": {"disabled_toolsets": ["terminal"]}}
        job = {"cron_toolset_exceptions": ["messaging"]}
        resolved = _resolve_cron_disabled_toolsets(cfg, job)
        assert resolved == ["cronjob", "clarify", "terminal"]


class TestGarbageInput:
    @pytest.mark.parametrize("bad", [42, 3.14, {"messaging": True}, object()])
    def test_non_string_non_sequence_types_ignored(self, bad):
        job = {"cron_toolset_exceptions": bad}
        assert _resolve_cron_disabled_toolsets({}, job) == BASE_DENYLIST
