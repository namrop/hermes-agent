"""Regression coverage for current Anthropic flagship model routing."""

from hermes_cli.model_switch import MODEL_ALIASES
from hermes_cli.models import OPENROUTER_MODELS, _PROVIDER_MODELS


def test_native_anthropic_catalog_includes_opus_48_and_fable_5_first():
    assert _PROVIDER_MODELS["anthropic"][:2] == [
        "claude-fable-5",
        "claude-opus-4-8",
    ]


def test_aggregator_catalogs_include_anthropic_opus_48_and_fable_5():
    openrouter_ids = [model for model, _desc in OPENROUTER_MODELS]
    assert openrouter_ids[:2] == [
        "anthropic/claude-fable-5",
        "anthropic/claude-opus-4.8",
    ]
    assert _PROVIDER_MODELS["nous"][:2] == [
        "anthropic/claude-fable-5",
        "anthropic/claude-opus-4.8",
    ]


def test_fable_short_alias_is_registered():
    identity = MODEL_ALIASES["fable"]
    assert identity.vendor == "anthropic"
    assert identity.family == "claude-fable"
