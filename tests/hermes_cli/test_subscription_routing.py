"""OpenRouter subscription guard (keeper ruling 2026-09-24).

A typed ``/model`` name or nickname must not land on OpenRouter's
pay-per-token copy of a model a configured subscription serves; the
OpenRouter copy is picker-only. Incident: ``/model gpt-6-sol`` resolved to
``openrouter/openai/gpt-6-sol`` from a Codex session (~$40, 2026-09-23).

Hermetic: catalogs and the resolution chain are mocked (no network).
"""

from unittest.mock import patch

import pytest

from hermes_cli import subscription_routing as sr
from hermes_cli.model_switch import switch_model

SUBS = ["openai-codex", "custom:meridian-primary", "custom:meridian-yugen", "opencode-go"]
CATALOGS = {
    "openai-codex": ["gpt-6-astra", "gpt-6-sol", "gpt-5.6-sol"],
    "opencode-go": ["grok-4.7", "minimax-m3", "qwen3.8-max", "muse-spark-1.3-contributor"],
}
CUSTOM = [
    {"name": "meridian-primary", "models": {"claude-opus-5-5": {}, "claude-haiku-4-5": {}}},
    {"name": "meridian-yugen", "models": {"claude-opus-5-5": {}, "claude-haiku-4-5": {}}},
]


def _catalog(provider):
    return CATALOGS.get(provider, [])


@pytest.fixture(autouse=True)
def _no_aliases():
    with patch.object(sr, "_alias_models_by_provider", return_value={}):
        yield


@pytest.mark.parametrize(
    "a,b",
    [
        ("openai/gpt-6-sol", "gpt-6-sol"),
        ("anthropic/claude-opus-5.5", "claude-opus-5-5"),
        ("qwen/qwen3.8-max-0902", "qwen3.8-max"),
        ("Claude-Haiku-4.5", "claude-haiku-4-5"),
    ],
)
def test_normalize_same_model(a, b):
    assert sr.normalize_model_key(a) == sr.normalize_model_key(b)


@pytest.mark.parametrize(
    "a,b",
    [
        ("meta/muse-spark-1.3", "muse-spark-1.3-contributor"),
        ("thinkingmachines/inkling:free", "inkling"),
        ("openai/gpt-6-sol-pro", "gpt-6-sol"),
    ],
)
def test_normalize_different_model(a, b):
    assert sr.normalize_model_key(a) != sr.normalize_model_key(b)


def test_matches_follow_provider_order_and_config_catalogs():
    m = sr.subscription_matches(
        "anthropic/claude-opus-5.5", SUBS, custom_providers=CUSTOM, catalog_fn=_catalog
    )
    assert list(m.items()) == [
        ("custom:meridian-primary", "claude-opus-5-5"),
        ("custom:meridian-yugen", "claude-opus-5-5"),
    ]


def test_matches_include_nickname_targets_when_catalog_is_stale():
    with patch.object(sr, "_alias_models_by_provider", return_value={"openai-codex": ["gpt-6-luna"]}):
        m = sr.subscription_matches("openai/gpt-6-luna", SUBS, catalog_fn=_catalog)
    assert m == {"openai-codex": "gpt-6-luna"}


def test_catalog_error_is_skipped_not_guessed():
    def boom(provider):
        raise RuntimeError("offline")

    assert sr.subscription_matches("openai/gpt-6-sol", ["openai-codex"], catalog_fn=boom) == {}


def _decide(model, **kw):
    kw.setdefault("subscription_providers", SUBS)
    kw.setdefault("custom_providers", CUSTOM)
    kw.setdefault("catalog_fn", _catalog)
    return sr.decide_openrouter_selection(model, **kw)


def test_typed_subscription_model_reroutes():
    d = _decide("openai/gpt-6-sol", typed_input="gpt-6-sol", current_provider="opencode-go")
    assert (d.action, d.provider, d.model) == ("reroute", "openai-codex", "gpt-6-sol")
    assert "picker-only" in d.message


def test_reroute_prefers_current_provider():
    d = _decide("anthropic/claude-haiku-4.5", current_provider="custom:meridian-yugen")
    assert (d.action, d.provider) == ("reroute", "custom:meridian-yugen")
    d = _decide("anthropic/claude-haiku-4.5", current_provider="openai-codex")
    assert (d.action, d.provider) == ("reroute", "custom:meridian-primary")


def test_typed_explicit_openrouter_is_refused():
    d = _decide("openai/gpt-6-sol", explicit_provider="openrouter")
    assert d.action == "refuse"
    assert "picker" in d.message and "--provider openai-codex" in d.message


def test_picker_and_config_selections_pass():
    for source in (sr.SELECTION_PICKER, sr.SELECTION_CONFIG):
        assert _decide("openai/gpt-6-sol", explicit_provider="openrouter",
                       selection_source=source).action == "allow"


def test_openrouter_only_model_passes():
    assert _decide("meta/muse-spark-1.3").action == "allow"
    assert _decide("google/gemini-3.8-flash").action == "allow"


def test_guard_off_without_configured_subscriptions():
    assert _decide("openai/gpt-6-sol", subscription_providers=[]).action == "allow"


def test_configured_subscription_providers_parsing():
    cfg = {"model_routing": {"subscription_providers": ["openai-codex", " zai ", "openrouter", "zai", 3]}}
    assert sr.configured_subscription_providers(cfg) == ["openai-codex", "zai"]
    assert sr.configured_subscription_providers({}) == []


# --- switch_model integration -------------------------------------------------

_ACCEPTED = {"accepted": True, "persist": True, "recognized": True, "message": None}


def _run_switch(raw_input, *, current_provider="openai-codex", explicit_provider="",
                selection_source="typed", detected=("openrouter", "openai/gpt-6-sol")):
    with patch("hermes_cli.model_switch.resolve_alias", return_value=None), \
         patch("hermes_cli.model_switch.list_provider_models", return_value=[]), \
         patch("hermes_cli.model_switch.normalize_model_for_provider", side_effect=lambda model, provider: model), \
         patch("hermes_cli.models.validate_requested_model", return_value=_ACCEPTED), \
         patch("hermes_cli.models.detect_provider_for_model", return_value=detected), \
         patch("hermes_cli.model_switch.get_model_info", return_value=None), \
         patch("hermes_cli.model_switch.get_model_capabilities", return_value=None), \
         patch("hermes_cli.subscription_routing.configured_subscription_providers", return_value=SUBS), \
         patch("hermes_cli.models.provider_model_ids", side_effect=_catalog), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               return_value={"api_key": "***", "base_url": "http://resolved/v1", "api_mode": ""}):
        return switch_model(
            raw_input=raw_input,
            current_provider=current_provider,
            current_model="old-model",
            explicit_provider=explicit_provider,
            user_providers={},
            custom_providers=CUSTOM,
            selection_source=selection_source,
        )


def test_switch_typed_gpt6_sol_stays_on_codex():
    r = _run_switch("gpt-6-sol", current_provider="opencode-go")
    assert r.success
    assert (r.target_provider, r.new_model) == ("openai-codex", "gpt-6-sol")
    assert "picker-only" in r.warning_message


def test_switch_typed_explicit_openrouter_refused():
    r = _run_switch("openai/gpt-6-sol", explicit_provider="openrouter")
    assert not r.success
    assert "picker" in r.error_message


def test_switch_picker_openrouter_allowed():
    r = _run_switch("openai/gpt-6-sol", explicit_provider="openrouter", selection_source="picker")
    assert r.success
    assert r.target_provider == "openrouter"
    assert r.new_model == "openai/gpt-6-sol"


def test_switch_openrouter_only_model_unchanged():
    r = _run_switch("muse-spark-1.3", detected=("openrouter", "meta/muse-spark-1.3"))
    assert r.success
    assert (r.target_provider, r.new_model) == ("openrouter", "meta/muse-spark-1.3")
    assert "picker-only" not in (r.warning_message or "")
