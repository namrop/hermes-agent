"""Earthglass authenticated rendered extraction provider."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from plugins.web.rendered_cdp import CdpRenderedExtractProvider, _load_web_config


class EarthglassWebExtractProvider(CdpRenderedExtractProvider):
    """Extract rendered pages from Luis-approved Earthglass Chrome CDP."""

    lane = "authenticated"
    authenticated = True
    env_names = ["EARTHGLASS_CDP_URL"]
    config_paths = [["earthglass", "endpoint"], ["authenticated_extract", "endpoint"]]
    default_endpoint = "http://127.0.0.1:9223"

    @property
    def name(self) -> str:
        return "earthglass"

    @property
    def display_name(self) -> str:
        return "Earthglass authenticated browser"

    def is_available(self) -> bool:
        cfg = _load_web_config()
        earthglass_cfg = cfg.get("earthglass", {}) if isinstance(cfg.get("earthglass"), dict) else {}
        if earthglass_cfg.get("enabled") is False:
            return False
        # Cheap only: no network probe. The script existing means this host has
        # the Earthglass lane installed; extraction still verifies live CDP.
        return bool(self.endpoint) and any(path.exists() for path in (Path.home().joinpath(".hermes/scripts/earthglass.sh"), Path.home().joinpath("hermes-primary-rsync/scripts/earthglass.sh")))

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "local · authenticated · explicit",
            "tag": (
                "Rendered extraction through the opt-in Earthglass Chrome profile. "
                "Use for signed-in/session/location-gated pages, not ordinary public fetches."
            ),
            "env_vars": [
                {
                    "key": "EARTHGLASS_CDP_URL",
                    "prompt": "Earthglass CDP endpoint",
                    "url": "http://127.0.0.1:9223",
                },
            ],
        }
