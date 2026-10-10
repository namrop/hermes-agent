"""The reasoning level over ACP: read at session start, offered, applied.

Before this, an ACP session (T3 Code) never read ``agent.reasoning_effort``:
its agent was built with no reasoning config, so the provider default applied
until a fallback hop re-resolved it. The client also had no way to choose a
level: Hermes advertised no settings, and stored any other config option it
was sent without using it.

Approved by Luis 2026-10-09, Discord #gateway "Model Fallback Chains" thread,
message 1558275641055514736 ("Yeah let's build it"), on the plan: read the
configured level when a session starts, offer a reasoning setting to the
client, and apply what the client sends back.
"""

import copy
from unittest.mock import AsyncMock, MagicMock

import acp
import pytest
from acp.schema import SetSessionConfigOptionResponse, TextContentBlock

from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager

BASE_CONFIG = {
    "model": {"default": "claude-opus-5-5", "provider": "custom:meridian-yugen"},
    "agent": {"reasoning_effort": "xhigh"},
    "mcp_servers": {},
}


@pytest.fixture()
def config(monkeypatch):
    cfg = copy.deepcopy(BASE_CONFIG)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    return cfg


@pytest.fixture()
def agent(config):
    manager = SessionManager(agent_factory=lambda: MagicMock(name="MockAIAgent"))
    return HermesACPAgent(session_manager=manager)


def _option(options, option_id):
    return next(option for option in options if option.id == option_id)


async def _new(agent):
    resp = await agent.new_session(cwd="/tmp")
    state = agent.session_manager.get_session(resp.session_id)
    state.agent.model = "claude-opus-5-5"
    state.agent.provider = "custom:meridian-yugen"
    return resp, state


# ---------------------------------------------------------------------------
# Offered: the session lists its settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_session_offers_a_reasoning_setting_that_defaults_to_config(agent):
    resp, _state = await _new(agent)

    reasoning = _option(resp.config_options, "reasoning")
    assert reasoning.category == "thought_level"
    assert reasoning.current_value == "default"
    assert [choice.value for choice in reasoning.options] == [
        "default", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
    ]
    assert reasoning.options[0].name == "Default (xhigh)"


@pytest.mark.asyncio
async def test_settings_also_list_the_mode_so_settings_clients_keep_it(agent):
    """The ACP spec tells clients that read settings to ignore ``modes``."""
    resp, _state = await _new(agent)

    assert [option.id for option in resp.config_options] == ["mode", "reasoning"]
    mode = _option(resp.config_options, "mode")
    assert mode.category == "mode"
    assert mode.current_value == "default"
    assert [choice.value for choice in mode.options] == [
        m.id for m in resp.modes.available_modes
    ]
    # Clients that read modes still get them.
    assert resp.modes.current_mode_id == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured", "label"),
    [("none", "Default (off)"), (False, "Default (off)"), ("", "Default (provider's own)")],
)
async def test_default_choice_names_what_the_config_resolves_to(agent, config, configured, label):
    config["agent"]["reasoning_effort"] = configured
    resp, _state = await _new(agent)

    assert _option(resp.config_options, "reasoning").options[0].name == label


@pytest.mark.asyncio
async def test_default_choice_follows_a_per_model_override(agent, config):
    config["agent"]["reasoning_overrides"] = {"claude-opus-5-5": "high"}
    resp, _state = await _new(agent)

    assert _option(resp.config_options, "reasoning").options[0].name == "Default (high)"


@pytest.mark.asyncio
async def test_load_and_resume_report_the_sessions_current_choice(agent):
    resp, _state = await _new(agent)
    await agent.set_config_option("reasoning", resp.session_id, "low")

    loaded = await agent.load_session(cwd="/tmp", session_id=resp.session_id)
    resumed = await agent.resume_session(cwd="/tmp", session_id=resp.session_id)

    assert _option(loaded.config_options, "reasoning").current_value == "low"
    assert _option(resumed.config_options, "reasoning").current_value == "low"


# ---------------------------------------------------------------------------
# Applied: what the client sends back
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_choosing_a_level_applies_it_and_reports_every_setting(agent):
    resp, state = await _new(agent)
    state.agent._primary_runtime = {"reasoning_config": {"enabled": True, "effort": "xhigh"}}

    update = await agent.set_config_option("reasoning", resp.session_id, "low")

    assert isinstance(update, SetSessionConfigOptionResponse)
    assert _option(update.config_options, "reasoning").current_value == "low"
    assert _option(update.config_options, "mode").current_value == "default"
    assert state.reasoning_effort == "low"
    assert state.agent.reasoning_config == {"enabled": True, "effort": "low"}
    # The next turn's primary restore must not put the old level back.
    assert state.agent._primary_runtime["reasoning_config"] == {"enabled": True, "effort": "low"}
    assert state.agent._session_reasoning_pinned is True


@pytest.mark.asyncio
async def test_choosing_off_disables_reasoning(agent):
    resp, state = await _new(agent)

    await agent.set_config_option("reasoning", resp.session_id, "none")

    assert state.agent.reasoning_config == {"enabled": False}


@pytest.mark.asyncio
async def test_choosing_default_goes_back_to_the_config(agent):
    resp, state = await _new(agent)
    await agent.set_config_option("reasoning", resp.session_id, "low")

    update = await agent.set_config_option("reasoning", resp.session_id, "default")

    assert state.reasoning_effort is None
    assert state.agent.reasoning_config == {"enabled": True, "effort": "xhigh"}
    assert state.agent._session_reasoning_pinned is False
    assert _option(update.config_options, "reasoning").current_value == "default"


@pytest.mark.asyncio
async def test_an_unknown_level_is_refused_and_changes_nothing(agent):
    resp, state = await _new(agent)
    await agent.set_config_option("reasoning", resp.session_id, "low")

    with pytest.raises(acp.RequestError) as excinfo:
        await agent.set_config_option("reasoning", resp.session_id, "extreme")

    assert excinfo.value.code == -32602
    assert state.reasoning_effort == "low"
    assert state.agent.reasoning_config == {"enabled": True, "effort": "low"}


@pytest.mark.asyncio
async def test_mode_setting_now_reports_every_setting(agent):
    resp, _state = await _new(agent)

    update = await agent.set_config_option("mode", resp.session_id, "dont_ask")

    assert _option(update.config_options, "mode").current_value == "dont_ask"
    assert _option(update.config_options, "reasoning").current_value == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["protocol", "slash"])
async def test_a_model_switch_keeps_the_sessions_choice(agent, switch):
    resp, state = await _new(agent)
    state.agent.provider = "anthropic"
    await agent.set_config_option("reasoning", resp.session_id, "low")
    old_agent = state.agent

    if switch == "protocol":
        await agent.set_session_model("anthropic:claude-opus-5-5", resp.session_id)
    else:
        agent._handle_slash_command("/model anthropic:claude-opus-5-5", state)

    assert state.agent is not old_agent
    assert state.agent.reasoning_config == {"enabled": True, "effort": "low"}
    assert state.agent._session_reasoning_pinned is True


# ---------------------------------------------------------------------------
# Each turn starts on the session's level
# ---------------------------------------------------------------------------


async def _prompt_capturing_reasoning(agent, state, session_id):
    seen = {}

    def _run(*_args, **_kwargs):
        seen["reasoning"] = state.agent.reasoning_config
        return {"final_response": "ok", "messages": []}

    state.agent.run_conversation = _run
    state.agent._supports_active_turn_redirect = False
    conn = MagicMock(spec=acp.Client)
    conn.session_update = AsyncMock()
    agent._conn = conn
    await agent.prompt(prompt=[TextContentBlock(type="text", text="hi")], session_id=session_id)
    return seen.get("reasoning")


@pytest.mark.asyncio
async def test_a_turn_starts_on_the_chosen_level_after_a_fallback_changed_it(agent):
    resp, state = await _new(agent)
    await agent.set_config_option("reasoning", resp.session_id, "low")
    # A fallback hop in an earlier turn re-resolved from config.
    state.agent.reasoning_config = {"enabled": True, "effort": "xhigh"}

    seen = await _prompt_capturing_reasoning(agent, state, resp.session_id)

    assert seen == {"enabled": True, "effort": "low"}


@pytest.mark.asyncio
async def test_a_turn_without_a_choice_picks_up_a_config_change(agent, config):
    resp, state = await _new(agent)
    config["agent"]["reasoning_effort"] = "medium"

    seen = await _prompt_capturing_reasoning(agent, state, resp.session_id)

    assert seen == {"enabled": True, "effort": "medium"}
