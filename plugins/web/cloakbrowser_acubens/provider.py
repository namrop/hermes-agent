"""Acubens CloakBrowser stealth rendered extraction provider."""

from __future__ import annotations

from typing import Any, Dict

from plugins.web.rendered_cdp import CdpRenderedExtractProvider


class CloakBrowserAcubensWebExtractProvider(CdpRenderedExtractProvider):
    """Extract rendered public pages through an Acubens-hosted stealth CDP browser."""

    lane = "stealth"
    authenticated = False
    env_names = [
        "CLOAKBROWSER_ACUBENS_CDP_URL",
        "CLOAKBROWSER_CDP_URL",
        "STEALTH_BROWSER_CDP_URL",
    ]
    config_paths = [["cloakbrowser_acubens", "endpoint"], ["stealth_extract", "endpoint"]]
    default_endpoint = ""

    @property
    def name(self) -> str:
        return "cloakbrowser-acubens"

    @property
    def display_name(self) -> str:
        return "Acubens CloakBrowser stealth browser"

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "local · stealth · explicit",
            "tag": (
                "Rendered extraction through a configured CloakBrowser/stealth CDP endpoint. "
                "Use for public pages that punish obvious automation; do not mix with real user profiles by default."
            ),
            "env_vars": [
                {
                    "key": "CLOAKBROWSER_ACUBENS_CDP_URL",
                    "prompt": "Acubens CloakBrowser CDP endpoint",
                    "url": "http://127.0.0.1:49225?fingerprint=hermes-spike",
                },
            ],
        }
