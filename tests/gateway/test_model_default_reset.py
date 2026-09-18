"""Gateway contract for the conversation-scoped ``/model default`` reset.

The command must clear only this conversation's explicit /model selection.  It
must not rotate its session, delete history, change personas, alter channel or
global configuration, or touch another conversation.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from gateway.config import ChannelOverride, GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore, build_session_key


OVERRIDE = {
    "model": "manual/model",
    "provider": "openrouter",
    "api_key": "not-persisted",
    "base_url": "https://example.invalid/v1",
    "api_mode": "chat_completions",
}


def _source(thread_id: str) -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-1",
        parent_chat_id="channel-1",
        thread_id=thread_id,
        user_id="user-1",
        chat_type="channel",
    )


def _event(args: str, *, thread_id: str = "thread-a") -> MessageEvent:
    return MessageEvent(
        text=f"/model {args}",
        message_type=MessageType.TEXT,
        source=_source(thread_id),
    )


def _runner(
    tmp_path, monkeypatch, *, use_sqlite: bool = False
) -> tuple[GatewayRunner, SessionStore]:
    """Build an isolated store; default to JSON-only for focused failure seams."""
    import gateway.run as gateway_run
    import hermes_state

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {"default": "global/model", "provider": "openrouter"},
                "discord": {
                    "channel_overrides": {
                        "channel-1": {
                            "model": "channel/model",
                            "provider": "anthropic",
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    if not use_sqlite:
        monkeypatch.setattr(
            hermes_state,
            "SessionDB",
            lambda: (_ for _ in ()).throw(RuntimeError("test JSON store")),
        )

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                channel_overrides={
                    "channel-1": ChannelOverride(
                        model="channel/model",
                        provider="anthropic",
                    )
                },
            )
        }
    )
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_model_overrides = {}
    runner._pending_one_turn_model_restores = {}
    runner._pending_model_notes = {}
    runner._last_resolved_model = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._session_db = None
    store = SessionStore(sessions_dir=hermes_home / "sessions", config=runner.config)
    runner.session_store = store
    return runner, store


def _seed_override(runner: GatewayRunner, store: SessionStore, source: SessionSource):
    entry = store.get_or_create_session(source)
    key = entry.session_key
    store.set_model_override(key, OVERRIDE)
    runner._session_model_overrides[key] = dict(OVERRIDE)
    runner._pending_one_turn_model_restores[key] = {
        "had_override": True,
        "override": dict(OVERRIDE),
    }
    runner._pending_model_notes[key] = "[Note: model was switched.]"
    runner._last_resolved_model[key] = OVERRIDE["model"]
    runner._session_state(key).conversation.personality_override = "writer"
    runner._agent_cache[key] = (None, None)
    return entry, key


@pytest.mark.asyncio
async def test_model_default_clears_only_this_thread_after_durable_readback(tmp_path, monkeypatch):
    """The real handler clears DB/JSON first, then volatile routing state."""
    runner, store = _runner(tmp_path, monkeypatch)
    current_entry, current_key = _seed_override(runner, store, _source("thread-a"))
    other_entry, other_key = _seed_override(runner, store, _source("thread-b"))

    # Before this feature, this reaches model selection rather than reset.
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("default must reset before model selection")),
    )

    reply = await runner._handle_model_command(_event("default"))

    assert reply is not None and "configured channel or global default" in reply.lower()
    assert store.get_model_override(current_key) is None
    # A fresh store reads the JSON durable record rather than process memory.
    reloaded = SessionStore(sessions_dir=store.sessions_dir, config=runner.config)
    assert reloaded.get_model_override(current_key) is None
    sessions_json = json.loads((store.sessions_dir / "sessions.json").read_text(encoding="utf-8"))
    assert sessions_json[current_key].get("model_override") is None
    assert current_key not in runner._session_model_overrides
    assert current_key not in runner._pending_one_turn_model_restores
    assert current_key not in runner._pending_model_notes
    assert current_key not in runner._last_resolved_model
    assert current_key not in runner._agent_cache
    assert runner._session_state(current_key).conversation.personality_override == "writer"
    assert store.get_or_create_session(_source("thread-a")).session_id == current_entry.session_id

    assert store.get_model_override(other_key) == {
        "model": "manual/model",
        "provider": "openrouter",
        "base_url": "https://example.invalid/v1",
    }
    assert runner._session_model_overrides[other_key]["model"] == "manual/model"
    assert store.get_or_create_session(_source("thread-b")).session_id == other_entry.session_id


@pytest.mark.asyncio
async def test_model_default_is_idempotent_and_falls_back_to_channel_default(tmp_path, monkeypatch):
    """Repeated resets remain local and restore normal channel precedence."""
    runner, store = _runner(tmp_path, monkeypatch)
    _entry, key = _seed_override(runner, store, _source("thread-a"))
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("default must not select a model")),
    )

    first = await runner._handle_model_command(_event("default"))
    second = await runner._handle_model_command(_event("default"))

    assert "configured channel or global default" in first.lower()
    assert "configured channel or global default" in second.lower()
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_key": "global-key",
            "base_url": "https://example.invalid/v1",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs_for_provider",
        lambda provider: {
            "provider": provider,
            "api_key": "channel-key",
            "base_url": "https://example.invalid/v1",
            "api_mode": "chat_completions",
        },
    )
    model, runtime = runner._resolve_session_agent_runtime(
        source=_source("thread-a"),
        session_key=key,
        user_config={"model": {"default": "global/model", "provider": "openrouter"}},
    )
    assert model == "channel/model"
    assert runtime["provider"] == "anthropic"
    assert store.get_model_override(key) is None


@pytest.mark.asyncio
async def test_model_default_clears_sqlite_and_json_mirror_for_a_fresh_runner(
    tmp_path, monkeypatch
):
    """A restart sees neither the SQLite routing row nor sessions.json override."""
    runner, store = _runner(tmp_path, monkeypatch, use_sqlite=True)
    assert store._db is not None
    _entry, key = _seed_override(runner, store, _source("thread-a"))
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("default must not select a model")),
    )

    reply = await runner._handle_model_command(_event("default"))

    assert reply is not None and "configured channel or global default" in reply.lower()
    assert Path(store._db.db_path).exists()
    fresh_store = SessionStore(sessions_dir=store.sessions_dir, config=runner.config)
    assert fresh_store._db is not None
    assert fresh_store.get_model_override(key) is None
    mirror = json.loads((store.sessions_dir / "sessions.json").read_text(encoding="utf-8"))
    assert mirror[key].get("model_override") is None
    fresh_runner = object.__new__(GatewayRunner)
    fresh_runner.session_store = fresh_store
    fresh_runner._session_model_overrides = {}
    fresh_runner._rehydrate_session_model_override(key)
    assert key not in fresh_runner._session_model_overrides


@pytest.mark.asyncio
async def test_model_default_refuses_success_when_sqlite_write_only_reaches_json_mirror(
    tmp_path, monkeypatch
):
    """A swallowed canonical-DB failure must not look like a durable reset."""
    runner, store = _runner(tmp_path, monkeypatch, use_sqlite=True)
    _entry, key = _seed_override(runner, store, _source("thread-a"))

    def _db_write_failure(*_args, **_kwargs):
        raise OSError("canonical routing database unavailable")

    monkeypatch.setattr(store._db, "replace_gateway_routing_entries", _db_write_failure)
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("default must not select a model")),
    )

    reply = await runner._handle_model_command(_event("default"))

    assert reply is not None and "could not verify" in reply.lower()
    assert "left unchanged" not in reply.lower()
    # Volatile runner state remains on the old route until an operator resolves
    # the ambiguous durable write; do not evict it into the disagreeing mirror.
    assert runner._session_model_overrides[key]["model"] == "manual/model"
    assert key in runner._pending_one_turn_model_restores
    assert key in runner._agent_cache


@pytest.mark.asyncio
async def test_runner_reset_helper_rejects_a_busy_target_without_mutation(tmp_path, monkeypatch):
    """The reusable exact-key helper must preserve a running conversation."""
    runner, store = _runner(tmp_path, monkeypatch)
    _entry, key = _seed_override(runner, store, _source("thread-a"))
    runner._running_agents[key] = object()

    with pytest.raises(RuntimeError, match="busy"):
        await runner._reset_session_model_override(key)

    assert store.get_model_override(key) is not None
    assert runner._session_model_overrides[key]["model"] == "manual/model"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["--global", "--once"])
async def test_model_default_rejects_widening_scopes(tmp_path, monkeypatch, scope):
    runner, store = _runner(tmp_path, monkeypatch)
    _entry, key = _seed_override(runner, store, _source("thread-a"))
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("scope conflict must not select a model")),
    )

    reply = await runner._handle_model_command(_event(f"default {scope}"))

    assert reply is not None and "cannot" in reply.lower()
    assert store.get_model_override(key) is not None
    assert runner._session_model_overrides[key]["model"] == "manual/model"


@pytest.mark.asyncio
async def test_model_default_leaves_memory_untouched_when_durable_clear_fails(tmp_path, monkeypatch):
    """A failed store clear must never report success or erase recoverable state."""
    runner, store = _runner(tmp_path, monkeypatch)
    _entry, key = _seed_override(runner, store, _source("thread-a"))
    runner._async_session_store = type(
        "FailingStore",
        (),
        {
            "_store": store,
            "get_durable_model_override": AsyncMock(
                return_value={
                    "model": "manual/model",
                    "provider": "openrouter",
                    "base_url": "https://example.invalid/v1",
                }
            ),
            "set_model_override": AsyncMock(side_effect=OSError("disk unavailable")),
        },
    )()
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("default must not select a model")),
    )

    reply = await runner._handle_model_command(_event("default"))

    assert reply is not None and "could not" in reply.lower()
    assert runner._session_model_overrides[key]["model"] == "manual/model"
    assert key in runner._pending_one_turn_model_restores
    assert key in runner._agent_cache


@pytest.mark.asyncio
async def test_provider_qualified_default_stays_a_deliberate_selection(tmp_path, monkeypatch):
    """Only bare gateway ``default`` is reserved; provider-qualified is a model."""
    from hermes_cli.model_switch import ModelSwitchResult

    runner, _store = _runner(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **kw: calls.append(kw) or ModelSwitchResult(
            success=True,
            new_model="default",
            target_provider="openai-codex",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            api_mode="chat_completions",
            provider_label="OpenAI Codex",
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.model_selection_guards.combined_selection_warning",
        lambda **_kw: None,
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.resolve_display_context_length",
        lambda *_args, **_kwargs: None,
    )

    reply = await runner._handle_model_command(_event("default --provider openai-codex"))

    assert reply is not None and "default" in reply.lower()
    assert calls and calls[0]["raw_input"] == "default"
    assert calls[0]["explicit_provider"] == "openai-codex"
