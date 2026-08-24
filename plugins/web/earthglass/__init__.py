"""Earthglass authenticated rendered extraction plugin."""

from __future__ import annotations

from plugins.web.earthglass.provider import EarthglassWebExtractProvider


def register(ctx) -> None:
    """Register Earthglass as an explicit rendered web extraction provider."""
    ctx.register_web_search_provider(EarthglassWebExtractProvider())
