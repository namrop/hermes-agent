#!/usr/bin/env python3
"""Tests for the openai-codex image generation provider.

Covers the GPT-Image-2.5 catalog expansion (2026-09-10): per-tier
``api_model`` pinning inside the Responses ``image_generation`` tool
config, legacy ``gpt-image-2`` tier compatibility, resolver precedence,
and prompt-level transparent-background detection.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module loading (plugin lives outside the package tree)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLUGIN_PATH = _REPO_ROOT / "plugins" / "image_gen" / "openai-codex" / "__init__.py"


@pytest.fixture(scope="module")
def plugin():
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    spec = importlib.util.spec_from_file_location("oc_plugin_under_test", _PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Catalog shape
# ---------------------------------------------------------------------------


class TestCatalog:
    def test_both_generations_present(self, plugin):
        ids = set(plugin._MODELS)
        assert "gpt-image-2.5-flare-low" in ids
        assert "gpt-image-2.5-flare-medium" in ids
        assert "gpt-image-2.5-flare-high" in ids
        assert "gpt-image-2.5-sunburst-medium" in ids
        assert "gpt-image-2.5-sunburst-high" in ids
        # legacy generation fully retained
        assert {"gpt-image-2-low", "gpt-image-2-medium", "gpt-image-2-high"} <= ids

    def test_every_tier_pins_api_model(self, plugin):
        for tier_id, meta in plugin._MODELS.items():
            assert "api_model" in meta, tier_id
            assert meta["api_model"] in (
                "gpt-image-2",
                "gpt-image-2.5-flare",
                "gpt-image-2.5-sunburst",
            ), (tier_id, meta["api_model"])
            assert meta["quality"] in ("low", "medium", "high"), tier_id

    def test_default_is_2_5_flare_medium(self, plugin):
        assert plugin.DEFAULT_MODEL == "gpt-image-2.5-flare-medium"
        assert plugin._MODELS[plugin.DEFAULT_MODEL]["api_model"] == "gpt-image-2.5-flare"


# ---------------------------------------------------------------------------
# Resolver precedence
# ---------------------------------------------------------------------------


class TestResolver:
    def test_env_override_wins(self, plugin, monkeypatch):
        monkeypatch.setenv("OPENAI_IMAGE_MODEL", "gpt-image-2.5-sunburst-high")
        tier, meta = plugin._resolve_model()
        assert tier == "gpt-image-2.5-sunburst-high"
        assert meta["api_model"] == "gpt-image-2.5-sunburst"

    def test_unknown_values_fall_back_to_default(self, plugin, monkeypatch):
        monkeypatch.delenv("OPENAI_IMAGE_MODEL", raising=False)
        tier, meta = plugin._resolve_model()
        # No config in the test environment: default tier
        assert tier == plugin.DEFAULT_MODEL
        assert meta is plugin._MODELS[plugin.DEFAULT_MODEL]

    def test_legacy_tier_id_still_resolves(self, plugin, monkeypatch):
        monkeypatch.setenv("OPENAI_IMAGE_MODEL", "gpt-image-2-high")
        tier, meta = plugin._resolve_model()
        assert tier == "gpt-image-2-high"
        assert meta["api_model"] == "gpt-image-2"


# ---------------------------------------------------------------------------
# Payload pinning
# ---------------------------------------------------------------------------


class TestPayload:
    def test_tool_config_pins_api_model(self, plugin):
        payload = plugin._build_responses_payload(
            prompt="a cat",
            size="1024x1024",
            quality="medium",
            api_model="gpt-image-2.5-sunburst",
            background="transparent",
        )
        tool = payload["tools"][0]
        assert tool["type"] == "image_generation"
        assert tool["model"] == "gpt-image-2.5-sunburst"
        assert tool["background"] == "transparent"
        assert tool["quality"] == "medium"
        # chat host model unchanged
        assert payload["model"] == plugin._CODEX_CHAT_MODEL

    def test_default_background_opaque(self, plugin):
        payload = plugin._build_responses_payload(
            prompt="a cat", size="1024x1024", quality="low",
            api_model="gpt-image-2.5-flare",
        )
        assert payload["tools"][0]["background"] == "opaque"

    def test_input_images_forwarded(self, plugin):
        payload = plugin._build_responses_payload(
            prompt="edit this",
            size="1024x1024",
            quality="high",
            api_model="gpt-image-2.5-flare",
            input_images=[{"type": "input_image", "image_url": "https://x/y.png"}],
        )
        content = payload["input"][0]["content"]
        assert content[0]["type"] == "input_text"
        assert content[1]["type"] == "input_image"


# ---------------------------------------------------------------------------
# Transparency prompt detection
# ---------------------------------------------------------------------------


class TestTransparencyDetection:
    @pytest.mark.parametrize("prompt,expected", [
        ("a sticker of a corgi, transparent background", True),
        ("sprite sheet for a 2d game, 16 frames", True),
        ("product cutout on white", True),  # cutout is a transparency verb
        ("a glass of transparent water", False),
        ("a busy street with signs", False),
        ("layers of atmosphere in a landscape", False),  # bare "layers" without transparent
        ("generate five sequential transparent layers foreground to background", True),
        ("png with alpha channel for compositing", True),
    ])
    def test_detection(self, plugin, prompt, expected):
        assert bool(plugin._TRANSPARENCY_PROMPT_RE.search(prompt)) is expected
