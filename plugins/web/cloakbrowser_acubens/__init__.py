"""Acubens CloakBrowser stealth rendered extraction plugin."""

from __future__ import annotations

from plugins.web.cloakbrowser_acubens.provider import CloakBrowserAcubensWebExtractProvider


def register(ctx) -> None:
    """Register the Acubens CloakBrowser extraction provider."""
    ctx.register_web_search_provider(CloakBrowserAcubensWebExtractProvider())
