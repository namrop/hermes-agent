"""``session_id_header`` — opt-in, provider-scoped conversation affinity.

A provider entry may name ONE request header that Hermes fills with a stable,
opaque, conversation-scoped id::

    providers:
      meridian-yugen:
        api: http://127.0.0.1:3457
        session_id_header: x-litellm-session-id

Why it exists: the deployed Meridian passthrough adapter reads
``x-litellm-session-id`` and nothing else. A request that arrives without it
is classified ``independent-request:headerless-tool-result`` and gets a fresh
upstream SDK session, which replays the whole history. A global/static value
would be worse than nothing — every lane would collapse into one session.

The contract these tests pin:

* absent / ``false`` / empty ⇒ no new header, for every provider;
* the value is opaque (never a raw session/user id) and stable for the life of
  one logical conversation — across tool rounds, retries, and agent
  re-instantiation/resume — up to the next COMMITTED context rewrite, which
  mints the next value (see test_affinity_compaction_generation.py);
* it differs across ``/new``, ``/branch`` forks, subagents, independent cron
  fires and unrelated sessions;
* it is scoped to ONE provider identity on ONE normalized route: a sibling
  provider sharing the base_url, or the same provider after a fallback to a
  different route, gets nothing;
* it rides in headers only — never in the request body — and never clobbers
  headers a transport or the user's config already built.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli.config import (
    _custom_provider_entry_to_provider_config,
    _normalize_custom_provider_entry,
    apply_custom_provider_extra_headers_to_client_kwargs,
    get_compatible_custom_providers,
    load_config,
)

HEADER = "x-litellm-session-id"
MERIDIAN_URL = "http://127.0.0.1:3457"
OTHER_URL = "https://api.anthropic.com"
_MSGS = [{"role": "user", "content": "hi"}]
_OPAQUE = re.compile(r"^hermes-[0-9a-f]{32}$")


# ── helpers ──────────────────────────────────────────────────────────────────


def _affinity():
    """Import the implementation module lazily.

    Kept out of module scope on purpose: during the RED run the module does
    not exist yet, and a top-level import would turn every assertion in this
    file into one collection error instead of individual failures.
    """
    import agent.provider_session_affinity as mod

    return mod


def _config(*entries, modern=False):
    """A config dict holding *entries* in the legacy or the v12+ shape."""
    if not modern:
        return {"custom_providers": [dict(e) for e in entries]}
    providers = {}
    for entry in entries:
        entry = dict(entry)
        name = entry.pop("name")
        entry["api"] = entry.pop("base_url")
        providers[name] = entry
    return {"providers": providers}


def _meridian_entry(**overrides):
    entry = {
        "name": "meridian-yugen",
        "base_url": MERIDIAN_URL,
        "api_mode": "anthropic_messages",
        "model": "claude-haiku-4-5",
        "session_id_header": HEADER,
        "extra_headers": {"x-meridian-agent": "passthrough"},
    }
    entry.update(overrides)
    return entry


def _stub_agent(
    provider="custom",
    requested_provider="custom:meridian-yugen",
    base_url=MERIDIAN_URL,
    session_id="sess-affinity-1",
    api_mode="chat_completions",
    session_db=None,
):
    return SimpleNamespace(
        provider=provider,
        requested_provider=requested_provider,
        base_url=base_url,
        session_id=session_id,
        api_mode=api_mode,
        _session_db=session_db,
    )


def _patch_mode_builder(monkeypatch, fake):
    """Swap the per-mode builder ``build_api_kwargs`` actually calls.

    Patched through the function's own ``__globals__`` (see the same helper in
    ``test_opencode_session_affinity.py``): a whole-directory run can import
    this module under a second identity, and a dotted-path setattr would then
    patch a module object the live function never reads.
    """
    from agent.chat_completion_helpers import build_api_kwargs

    monkeypatch.setitem(
        build_api_kwargs.__globals__, "_build_api_kwargs_for_mode", fake
    )


@pytest.fixture
def meridian_home(tmp_path, monkeypatch):
    """A real on-disk HERMES_HOME whose config opts one provider in."""

    def _write(config):
        home = tmp_path / ".hermes"
        home.mkdir(exist_ok=True)
        (home / "config.yaml").write_text(yaml.safe_dump(config))
        monkeypatch.setenv("HERMES_HOME", str(home))
        return home

    return _write


# ── 1. Config normalization — the opt-in survives both config shapes ─────────


class TestConfigNormalization:
    def test_entry_keeps_the_declared_header(self):
        normalized = _normalize_custom_provider_entry(_meridian_entry())
        assert normalized is not None
        assert normalized["session_id_header"] == HEADER

    def test_header_name_is_trimmed_and_case_folded(self):
        normalized = _normalize_custom_provider_entry(
            _meridian_entry(session_id_header="  X-LiteLLM-Session-Id \n")
        )
        assert normalized["session_id_header"] == HEADER

    @pytest.mark.parametrize(
        "value", [None, False, "", "   ", True, 42, {"name": HEADER}, ["x"]]
    )
    def test_absent_false_or_non_string_means_no_opt_in(self, value):
        entry = _meridian_entry()
        if value is None:
            entry.pop("session_id_header")
        else:
            entry["session_id_header"] = value
        normalized = _normalize_custom_provider_entry(entry)
        assert normalized is not None
        assert "session_id_header" not in normalized

    @pytest.mark.parametrize(
        "bad", ["x litellm session id", "x-litellm:session", "x\nsession", "sess id\t"]
    )
    def test_an_illegal_header_name_is_rejected(self, bad):
        """A name httpx would reject must never reach the request builder."""
        normalized = _normalize_custom_provider_entry(
            _meridian_entry(session_id_header=bad)
        )
        assert "session_id_header" not in normalized

    def test_the_key_is_known_config_not_an_ignored_unknown(self, caplog):
        with caplog.at_level("WARNING"):
            _normalize_custom_provider_entry(
                _meridian_entry(), provider_key="unknown-key-probe"
            )
        assert "session_id_header" not in caplog.text

    @pytest.mark.parametrize("modern", [False, True])
    def test_both_config_shapes_carry_the_opt_in(self, modern):
        entries = get_compatible_custom_providers(
            _config(_meridian_entry(), modern=modern)
        )
        assert [e["session_id_header"] for e in entries] == [HEADER]

    def test_legacy_to_v12_translation_preserves_the_opt_in(self):
        translated = _custom_provider_entry_to_provider_config(
            _meridian_entry(), provider_key="meridian-yugen"
        )
        assert translated["session_id_header"] == HEADER

    @pytest.mark.parametrize("modern", [False, True])
    def test_resolution_from_a_real_config_file(self, meridian_home, modern):
        meridian_home(_config(_meridian_entry(), modern=modern))
        from hermes_cli.config import get_custom_provider_session_id_header

        assert (
            get_custom_provider_session_id_header(
                "custom:meridian-yugen", MERIDIAN_URL, config=load_config()
            )
            == HEADER
        )


# ── 2. Scope guard — one identity, one normalized route ──────────────────────


class TestScopeGuard:
    def _resolve(self, provider, base_url, config):
        return _affinity().resolve_session_id_header(
            provider, base_url, config=config
        )

    def test_matching_identity_and_route_opts_in(self):
        cfg = _config(_meridian_entry())
        assert self._resolve("custom:meridian-yugen", MERIDIAN_URL, cfg) == HEADER

    def test_trailing_slash_and_case_are_the_same_route(self):
        cfg = _config(_meridian_entry())
        assert (
            self._resolve("custom:meridian-yugen", "http://127.0.0.1:3457/", cfg)
            == HEADER
        )

    def test_a_sibling_provider_on_the_same_base_url_is_not_opted_in(self):
        """The opt-in belongs to an identity, not to a URL."""
        cfg = _config(
            _meridian_entry(),
            _meridian_entry(name="meridian-plain", session_id_header=False),
        )
        assert self._resolve("custom:meridian-plain", MERIDIAN_URL, cfg) is None
        assert self._resolve("custom:meridian-yugen", MERIDIAN_URL, cfg) == HEADER

    def test_an_ambiguous_bare_identity_fails_closed(self):
        """Two entries share the route and disagree — send nothing."""
        cfg = _config(
            _meridian_entry(),
            _meridian_entry(name="meridian-plain", session_id_header=False),
        )
        assert self._resolve("custom", MERIDIAN_URL, cfg) is None

    def test_a_bare_identity_on_an_unambiguous_route_opts_in(self):
        """The live shape: provider='custom' with one entry on that route."""
        cfg = _config(_meridian_entry())
        assert self._resolve("custom", MERIDIAN_URL, cfg) == HEADER

    def test_another_route_gets_nothing(self):
        cfg = _config(_meridian_entry())
        assert self._resolve("custom:meridian-yugen", OTHER_URL, cfg) is None

    def test_an_unrelated_provider_identity_gets_nothing(self):
        cfg = _config(_meridian_entry())
        assert self._resolve("anthropic", MERIDIAN_URL, cfg) is None

    def test_no_opt_in_anywhere_is_silent(self):
        cfg = _config(_meridian_entry(session_id_header=False))
        assert self._resolve("custom:meridian-yugen", MERIDIAN_URL, cfg) is None


# ── 3. The value — opaque, conversation-scoped, rotation-stable ──────────────


class TestSessionKey:
    def test_the_value_is_opaque_and_leaks_no_identifier(self):
        raw = "discord_444333222111_thread_9"
        value = _affinity().session_affinity_value(raw)
        assert _OPAQUE.match(value)
        assert raw not in value
        assert "444333222111" not in value

    def test_the_same_scope_always_derives_the_same_value(self):
        mod = _affinity()
        assert mod.session_affinity_value("s-1") == mod.session_affinity_value("s-1")

    def test_the_derivation_is_not_salted_per_process(self):
        """Resume in a NEW process must land on the same upstream session."""
        code = textwrap.dedent(
            """
            import sys
            sys.path.insert(0, %r)
            from agent.provider_session_affinity import session_affinity_value
            print(session_affinity_value("sess-resume-7"))
            """
        ) % str(Path(__file__).resolve().parents[2])
        runs = {
            subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                check=True,
                env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
            ).stdout.strip()
            for seed in ("0", "1", "random")
        }
        assert runs == {_affinity().session_affinity_value("sess-resume-7")}

    def test_distinct_conversations_get_distinct_values(self):
        mod = _affinity()
        values = {
            mod.session_affinity_value(sid)
            for sid in ("sess-main", "sess-main-child-1", "sess-new-after-reset")
        }
        assert len(values) == 3

    def test_independent_cron_fires_get_distinct_values(self):
        mod = _affinity()
        assert mod.session_affinity_value(
            "cron_masthead_20260917_041500"
        ) != mod.session_affinity_value("cron_masthead_20260917_061500")

    def test_an_empty_scope_yields_no_value(self):
        mod = _affinity()
        assert mod.session_affinity_value("") == ""
        assert mod.session_affinity_value(None) == ""


class TestAgentScope:
    """The agent-level scope: what actually feeds the header."""

    def _value(self, agent):
        return _affinity().session_affinity_value_for_agent(agent)

    def test_a_resumed_agent_object_resolves_the_same_value(self):
        first = self._value(_stub_agent(session_id="sess-resume-7"))
        second = self._value(_stub_agent(session_id="sess-resume-7"))
        assert first == second == self._value(_stub_agent(session_id="sess-resume-7"))

    def test_a_committed_rotation_keeps_the_cache_root_but_moves_the_value(
        self, tmp_path
    ):
        """Rotation keeps ONE conversation but replaces its history.

        Two different identities are in play and they must not be conflated:
        the prompt-cache scope (the compression-lineage ROOT) stays put, so
        rotation does not churn the cache bucket; the affinity value does NOT,
        because a replaying proxy holds the PRE-compaction transcript under the
        old value and would resume it by suffix overlap, never seeing the
        compacted prefix. See tests/agent/test_affinity_compaction_generation.py
        for the full contract.
        """
        from agent.prompt_cache_scope import resolve_prompt_cache_scope
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            db.create_session("sess-root", source="cli")
            db.append_message("sess-root", "user", "long history")
            root = _stub_agent(session_id="sess-root", session_db=db)
            root_value = self._value(root)

            db.publish_compression_child(
                parent_session_id="sess-root",
                child_session_id="sess-root-rot2",
                source="cli",
                messages=[{"role": "user", "content": "[COMPACTION] summary"}],
                require_compression_lease=False,
            )
            rotated = _stub_agent(session_id="sess-root-rot2", session_db=db)

            assert resolve_prompt_cache_scope(rotated) == "sess-root"
            assert self._value(rotated) != root_value
        finally:
            db.close()

    def test_a_fork_child_does_not_inherit_the_parent_value(self):
        parent = _stub_agent(session_id="sess-root")
        child = _stub_agent(
            session_id="sess-child",
            session_db=SimpleNamespace(get_compression_lineage=lambda sid: ["sess-child"]),
        )
        assert self._value(child) != self._value(parent)


# ── 4. The request builder ───────────────────────────────────────────────────


class TestBuildApiKwargs:
    def _build(self, agent, monkeypatch, base=None, messages=None):
        from agent.chat_completion_helpers import build_api_kwargs

        _patch_mode_builder(
            monkeypatch,
            lambda a, msgs, tools=None: dict(base or {"model": "m", "messages": msgs}),
        )
        return build_api_kwargs(agent, _MSGS if messages is None else messages)

    def test_the_header_rides_on_an_opted_in_request(self, meridian_home, monkeypatch):
        meridian_home(_config(_meridian_entry()))
        kwargs = self._build(_stub_agent(), monkeypatch)
        assert kwargs["extra_headers"][HEADER] == _affinity().session_affinity_value(
            "sess-affinity-1"
        )

    def test_real_legacy_display_name_resolution_carries_header(
        self, meridian_home, monkeypatch
    ):
        from hermes_cli.runtime_provider import resolve_runtime_provider

        meridian_home(_config(_meridian_entry(name="Meridian Yugen")))
        runtime = resolve_runtime_provider(requested="custom:meridian-yugen")
        assert runtime["requested_provider"] == "custom:meridian-yugen"

        agent = _stub_agent(
            provider=runtime["provider"],
            requested_provider=runtime["requested_provider"],
            base_url=runtime["base_url"],
            api_mode=runtime["api_mode"],
        )
        kwargs = self._build(agent, monkeypatch)

        assert kwargs["extra_headers"][HEADER] == _affinity().session_affinity_value(
            "sess-affinity-1"
        )

    def test_without_the_opt_in_no_header_is_added(self, meridian_home, monkeypatch):
        meridian_home(_config(_meridian_entry(session_id_header=False)))
        kwargs = self._build(_stub_agent(), monkeypatch)
        assert HEADER not in (kwargs.get("extra_headers") or {})

    def test_an_unconfigured_provider_is_untouched(self, meridian_home, monkeypatch):
        meridian_home(_config(_meridian_entry()))
        agent = _stub_agent(
            provider="anthropic", requested_provider="anthropic", base_url=OTHER_URL
        )
        kwargs = self._build(agent, monkeypatch)
        assert "extra_headers" not in kwargs

    def test_the_value_is_stable_across_tool_rounds_and_retries(
        self, meridian_home, monkeypatch
    ):
        meridian_home(_config(_meridian_entry()))
        agent = _stub_agent()
        seen = set()
        messages = list(_MSGS)
        for round_no in range(3):
            kwargs = self._build(agent, monkeypatch, messages=messages)
            seen.add(kwargs["extra_headers"][HEADER])
            # grow the transcript the way a tool loop does, then "retry"
            messages += [
                {"role": "assistant", "content": None, "tool_calls": [{"id": f"t{round_no}"}]},
                {"role": "tool", "tool_call_id": f"t{round_no}", "content": "ok"},
            ]
            seen.add(
                self._build(agent, monkeypatch, messages=messages)["extra_headers"][HEADER]
            )
        assert len(seen) == 1

    def test_headers_a_transport_already_built_are_preserved(
        self, meridian_home, monkeypatch
    ):
        meridian_home(_config(_meridian_entry()))
        kwargs = self._build(
            _stub_agent(),
            monkeypatch,
            base={"extra_headers": {"anthropic-beta": "context-1m-2025-08-07"}},
        )
        assert kwargs["extra_headers"]["anthropic-beta"] == "context-1m-2025-08-07"
        assert kwargs["extra_headers"][HEADER]

    def test_a_pinned_value_already_on_the_request_wins(
        self, meridian_home, monkeypatch
    ):
        meridian_home(_config(_meridian_entry()))
        kwargs = self._build(
            _stub_agent(), monkeypatch, base={"extra_headers": {HEADER: "pinned"}}
        )
        assert kwargs["extra_headers"][HEADER] == "pinned"

    def test_a_fallback_to_another_route_strips_the_affinity(
        self, meridian_home, monkeypatch
    ):
        """Same live agent object, new route — the memo must not go stale."""
        meridian_home(_config(_meridian_entry()))
        agent = _stub_agent()
        assert self._build(agent, monkeypatch)["extra_headers"][HEADER]
        agent.base_url = OTHER_URL
        agent.provider = "anthropic"
        agent.requested_provider = "anthropic"
        assert HEADER not in (self._build(agent, monkeypatch).get("extra_headers") or {})

    def test_a_switch_back_restores_the_affinity(self, meridian_home, monkeypatch):
        meridian_home(_config(_meridian_entry()))
        agent = _stub_agent()
        self._build(agent, monkeypatch)
        agent.base_url = OTHER_URL
        self._build(agent, monkeypatch)
        agent.base_url = MERIDIAN_URL
        assert self._build(agent, monkeypatch)["extra_headers"][HEADER]

    def test_the_opt_in_never_becomes_a_body_field(self, meridian_home, monkeypatch):
        meridian_home(_config(_meridian_entry()))
        kwargs = self._build(_stub_agent(), monkeypatch)
        value = kwargs["extra_headers"][HEADER]
        body = {k: v for k, v in kwargs.items() if k != "extra_headers"}
        assert "session_id_header" not in kwargs
        assert HEADER not in json.dumps(body, default=str)
        assert value not in json.dumps(body, default=str)

    def test_the_real_anthropic_messages_build_carries_it(self, meridian_home):
        """No monkeypatched builder — the live transport builds the kwargs."""
        from agent.chat_completion_helpers import build_api_kwargs
        from agent.transports.anthropic import AnthropicTransport

        meridian_home(_config(_meridian_entry()))
        transport = AnthropicTransport()
        agent = _stub_agent(api_mode="anthropic_messages")
        agent.model = "claude-haiku-4-5"
        agent.tools = None
        agent.max_tokens = 256
        agent.reasoning_config = None
        agent.request_overrides = {}
        agent.context_compressor = None
        agent._ephemeral_max_output_tokens = None
        agent._is_anthropic_oauth = False
        agent._anthropic_base_url = MERIDIAN_URL
        agent._oauth_1m_beta_disabled = False
        agent._get_transport = lambda: transport
        agent._prepare_anthropic_messages_for_api = lambda msgs: msgs
        agent._anthropic_preserve_dots = lambda: False

        kwargs = build_api_kwargs(agent, _MSGS)
        assert kwargs["extra_headers"][HEADER] == _affinity().session_affinity_value(
            "sess-affinity-1"
        )


# ── 5. The HTTP boundary ─────────────────────────────────────────────────────


class TestWire:
    """Proof at the transport: what a recording httpx client actually sees.

    Mocked upstream — this proves Hermes emits the header on the wire with the
    configured client, NOT that a live Meridian accepts it.
    """

    def _recorder(self):
        import httpx

        seen = {}

        def handle(request: httpx.Request) -> httpx.Response:
            seen["headers"] = dict(request.headers)
            seen["header_items"] = list(request.headers.multi_items())
            seen["body"] = request.content.decode()
            return httpx.Response(
                200,
                json={
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-haiku-4-5",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )

        return seen, httpx.MockTransport(handle)

    def _anthropic_client(self, transport, config):
        import anthropic
        import httpx

        client_kwargs = {"api_key": "meridian-local", "base_url": MERIDIAN_URL}
        apply_custom_provider_extra_headers_to_client_kwargs(
            client_kwargs, MERIDIAN_URL, config=config
        )
        return anthropic.Anthropic(
            api_key=client_kwargs["api_key"],
            base_url=client_kwargs["base_url"],
            default_headers=client_kwargs.get("default_headers") or {},
            http_client=httpx.Client(transport=transport),
        )

    def _anthropic_agent(self):
        from agent.transports.anthropic import AnthropicTransport

        transport = AnthropicTransport()
        agent = _stub_agent(api_mode="anthropic_messages")
        agent.model = "claude-haiku-4-5"
        agent.tools = None
        agent.max_tokens = 256
        agent.reasoning_config = None
        agent.request_overrides = {}
        agent.context_compressor = None
        agent._ephemeral_max_output_tokens = None
        agent._is_anthropic_oauth = False
        agent._anthropic_base_url = MERIDIAN_URL
        agent._oauth_1m_beta_disabled = False
        agent._get_transport = lambda: transport
        agent._prepare_anthropic_messages_for_api = lambda msgs: msgs
        agent._anthropic_preserve_dots = lambda: False
        return agent

    def test_the_header_reaches_the_wire_beside_config_and_auth_headers(
        self, meridian_home
    ):
        from agent.chat_completion_helpers import build_api_kwargs

        config = _config(_meridian_entry())
        meridian_home(config)
        seen, transport = self._recorder()
        client = self._anthropic_client(transport, load_config())
        kwargs = build_api_kwargs(self._anthropic_agent(), _MSGS)
        client.messages.create(**kwargs)

        expected = _affinity().session_affinity_value("sess-affinity-1")
        assert seen["headers"][HEADER] == expected
        # the user's own per-provider header and SDK auth both survive
        assert seen["headers"]["x-meridian-agent"] == "passthrough"
        assert seen["headers"]["x-api-key"] == "meridian-local"
        # and the opt-in is nowhere in the body
        assert "session_id_header" not in seen["body"]
        assert HEADER not in seen["body"]
        assert expected not in seen["body"]

    def test_without_the_opt_in_the_wire_has_no_such_header(self, meridian_home):
        from agent.chat_completion_helpers import build_api_kwargs

        config = _config(_meridian_entry(session_id_header=False))
        meridian_home(config)
        seen, transport = self._recorder()
        client = self._anthropic_client(transport, load_config())
        client.messages.create(**build_api_kwargs(self._anthropic_agent(), _MSGS))
        assert HEADER not in seen["headers"]
        assert seen["headers"]["x-meridian-agent"] == "passthrough"

    def test_mixed_case_pinned_header_wins_on_the_anthropic_wire(
        self, meridian_home, monkeypatch
    ):
        from agent.chat_completion_helpers import build_api_kwargs

        config = _config(_meridian_entry())
        meridian_home(config)
        seen, transport = self._recorder()
        client = self._anthropic_client(transport, load_config())
        _patch_mode_builder(
            monkeypatch,
            lambda a, msgs, tools=None: {
                "model": "claude-haiku-4-5",
                "max_tokens": 256,
                "messages": msgs,
                "extra_headers": {
                    "X-LiteLLM-Session-Id": "pinned",
                    "X-Transport-Trace": "kept",
                },
            },
        )

        kwargs = build_api_kwargs(self._anthropic_agent(), _MSGS)
        client.messages.create(**kwargs)

        assert kwargs["extra_headers"] == {
            "X-LiteLLM-Session-Id": "pinned",
            "X-Transport-Trace": "kept",
        }
        assert seen["headers"][HEADER] == "pinned"
        assert [
            value
            for name, value in seen["header_items"]
            if name.lower() == HEADER
        ] == ["pinned"]
        assert seen["headers"]["x-transport-trace"] == "kept"

    def test_the_openai_wire_carries_it_too(self, meridian_home, monkeypatch):
        """chat_completions clients send ``extra_headers`` as real headers."""
        import httpx
        import openai

        from agent.chat_completion_helpers import build_api_kwargs

        meridian_home(_config(_meridian_entry(api_mode="chat_completions")))
        seen = {}

        def handle(request: httpx.Request) -> httpx.Response:
            seen["headers"] = dict(request.headers)
            seen["body"] = request.content.decode()
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "m",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        _patch_mode_builder(
            monkeypatch, lambda a, msgs, tools=None: {"model": "m", "messages": msgs}
        )
        kwargs = build_api_kwargs(_stub_agent(), _MSGS)
        client = openai.OpenAI(
            api_key="meridian-local",
            base_url=MERIDIAN_URL,
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
        client.chat.completions.create(**kwargs)

        expected = _affinity().session_affinity_value("sess-affinity-1")
        assert seen["headers"][HEADER] == expected
        assert expected not in seen["body"]

    def test_mixed_case_pinned_header_wins_on_the_openai_wire(
        self, meridian_home, monkeypatch
    ):
        import httpx
        import openai

        from agent.chat_completion_helpers import build_api_kwargs

        meridian_home(_config(_meridian_entry(api_mode="chat_completions")))
        seen = {}

        def handle(request: httpx.Request) -> httpx.Response:
            seen["headers"] = dict(request.headers)
            seen["header_items"] = list(request.headers.multi_items())
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "m",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        _patch_mode_builder(
            monkeypatch,
            lambda a, msgs, tools=None: {
                "model": "m",
                "messages": msgs,
                "extra_headers": {
                    "X-LiteLLM-Session-Id": "pinned",
                    "X-Transport-Trace": "kept",
                },
            },
        )
        kwargs = build_api_kwargs(_stub_agent(), _MSGS)
        client = openai.OpenAI(
            api_key="meridian-local",
            base_url=MERIDIAN_URL,
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
        client.chat.completions.create(**kwargs)

        assert kwargs["extra_headers"] == {
            "X-LiteLLM-Session-Id": "pinned",
            "X-Transport-Trace": "kept",
        }
        assert seen["headers"][HEADER] == "pinned"
        assert [
            value
            for name, value in seen["header_items"]
            if name.lower() == HEADER
        ] == ["pinned"]
        assert seen["headers"]["x-transport-trace"] == "kept"


# ── 6. Scope boundary: auxiliary calls are deliberately excluded ─────────────


def test_auxiliary_calls_do_not_carry_the_affinity_header(meridian_home):
    """Compression/title/vision traffic is NOT the conversation.

    Meridian keys an upstream SDK session on this header and replays history
    into it; an auxiliary call carries a different message list entirely, so
    pinning it to the conversation's session is a corruption risk we cannot
    verify from here. The main turn is what the reported symptom is about.
    """
    from agent import auxiliary_client as aux

    meridian_home(_config(_meridian_entry(api_mode="chat_completions")))
    token = aux.set_runtime_main(
        "custom",
        "claude-haiku-4-5",
        base_url=MERIDIAN_URL,
        session_id="sess-affinity-1",
    )
    try:
        kwargs = aux._build_call_kwargs(
            "custom", "claude-haiku-4-5", _MSGS, base_url=MERIDIAN_URL
        )
    finally:
        aux._RUNTIME_MAIN_CONTEXT.reset(token)
    assert HEADER not in (kwargs.get("extra_headers") or {})
