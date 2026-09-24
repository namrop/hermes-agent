"""Keep typed model selections off OpenRouter when a subscription serves the model.

Keeper ruling 2026-09-24 (Discord #locutus, msg 1552730543563608155): an
OpenRouter model that is also reachable through one of the operator's
subscriptions must not be reachable by a nickname or a typed name. Its
OpenRouter copy is selectable only from the interactive ``/model`` picker,
for emergencies.

Incident behind the ruling: ``/model gpt-6-sol`` resolved to OpenRouter's
pay-per-token copy (about $40 on 2026-09-23) because nothing claimed the id
before ``detect_provider_for_model`` fell through to the OpenRouter catalog,
even though the session was already on the Codex subscription.

What this module does, for a *typed* selection whose resolution landed on
OpenRouter:

- the same model is served by a configured subscription provider, no
  ``--provider`` flag: route to the subscription copy instead (current
  provider first, then ``subscription_providers`` order) and say so;
- the same model is served by a subscription, ``--provider openrouter`` typed:
  refuse and point at the picker;
- no subscription serves it (for example an OpenRouter-only model): allow.

Picker selections and config-driven resets are never touched.

Config (opt-in; an empty or missing list turns the guard off)::

    model_routing:
      subscription_providers:   # also the tie-break order
        - openai-codex
        - custom:meridian-primary
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

SELECTION_TYPED = "typed"
SELECTION_PICKER = "picker"
SELECTION_CONFIG = "config"

OPENROUTER = "openrouter"

# Dated snapshot suffixes: ``qwen3.8-max-0902``, ``claude-x-20250514``.
_SNAPSHOT_SUFFIX = re.compile(r"-(?:\d{4}|\d{8})$")


def normalize_model_key(model_id: str) -> str:
    """Comparison key for "is this the same model on another provider?".

    Drops the vendor prefix (``openai/``), lowercases, treats ``.`` and ``_``
    as ``-`` (``claude-opus-5.5`` == ``claude-opus-5-5``) and drops a trailing
    dated snapshot. Variant tags after ``:`` (``:free``) are kept, so a free
    OpenRouter variant never matches a paid subscription model name.
    """
    key = (model_id or "").strip().lower()
    if "/" in key:
        key = key.rsplit("/", 1)[1]
    key = key.replace(".", "-").replace("_", "-")
    return _SNAPSHOT_SUFFIX.sub("", key)


def configured_subscription_providers(cfg: Optional[dict] = None) -> list[str]:
    """Provider slugs listed under ``model_routing.subscription_providers``."""
    if cfg is None:
        try:
            from hermes_cli.config import load_config

            cfg = load_config()
        except Exception:  # noqa: BLE001 - config trouble must not break /model
            return []
    section = cfg.get("model_routing") if isinstance(cfg, dict) else None
    raw = section.get("subscription_providers") if isinstance(section, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        slug = item.strip()
        if slug.lower() in seen or slug.lower() == OPENROUTER:
            continue
        seen.add(slug.lower())
        out.append(slug)
    return out


def _declared_ids(value: Any) -> list[str]:
    from hermes_cli.model_switch import _declared_model_ids

    return _declared_model_ids(value)


def _configured_catalog(
    provider: str,
    user_providers: Optional[dict],
    custom_providers: Optional[list],
) -> Optional[list[str]]:
    """Models a user/custom provider declares in config, or None if not one."""
    if provider.startswith("custom:"):
        name = provider.split(":", 1)[1].strip()
        for entry in custom_providers or []:
            if isinstance(entry, dict) and str(entry.get("name", "")).strip() == name:
                ids: list[str] = []
                for key in ("models", "model", "default_model"):
                    ids.extend(_declared_ids(entry.get(key)))
                return ids
        return []
    if isinstance(user_providers, dict) and isinstance(user_providers.get(provider), dict):
        entry = user_providers[provider]
        ids = []
        for key in ("models", "model", "default_model"):
            ids.extend(_declared_ids(entry.get(key)))
        if ids:
            return ids
    return None


def _alias_models_by_provider() -> dict[str, list[str]]:
    """Models that configured nicknames already point at, per provider.

    A nickname aimed at a subscription is evidence the subscription serves the
    model even when the provider's catalog fetch is stale or offline (the
    Codex static list lacked ``gpt-6-*`` on 2026-09-23).
    """
    try:
        from hermes_cli.model_switch import DIRECT_ALIASES, _ensure_direct_aliases

        _ensure_direct_aliases()
        out: dict[str, list[str]] = {}
        for alias in DIRECT_ALIASES.values():
            out.setdefault(alias.provider, []).append(alias.model)
        return out
    except Exception:  # noqa: BLE001
        return {}


def subscription_matches(
    model_id: str,
    providers: Iterable[str],
    *,
    user_providers: Optional[dict] = None,
    custom_providers: Optional[list] = None,
    catalog_fn: Optional[Callable[[str], list[str]]] = None,
) -> dict[str, str]:
    """``{provider: native_model_id}`` for each provider that serves ``model_id``.

    Order follows ``providers``. A provider whose catalog cannot be read is
    skipped (logged), never guessed.
    """
    target = normalize_model_key(model_id)
    if not target:
        return {}
    fetch = catalog_fn
    if fetch is None:
        from hermes_cli.models import provider_model_ids

        fetch = provider_model_ids
    alias_models = _alias_models_by_provider()
    matches: dict[str, str] = {}
    for provider in providers:
        catalog = _configured_catalog(provider, user_providers, custom_providers)
        if catalog is None:
            try:
                catalog = list(fetch(provider) or [])
            except Exception as exc:  # noqa: BLE001
                logger.warning("subscription catalog for %s unavailable: %s", provider, exc)
                catalog = []
        catalog = list(catalog) + list(alias_models.get(provider, []))
        for native in catalog:
            if isinstance(native, str) and normalize_model_key(native) == target:
                matches[provider] = native
                break
    return matches


@dataclass(frozen=True)
class OpenRouterSelectionDecision:
    """What :func:`hermes_cli.model_switch.switch_model` should do."""

    action: str  # "allow" | "reroute" | "refuse"
    provider: str = ""
    model: str = ""
    message: str = ""


_ALLOW = OpenRouterSelectionDecision(action="allow")


def _label(provider: str) -> str:
    try:
        from hermes_cli.providers import get_label

        return get_label(provider) or provider
    except Exception:  # noqa: BLE001
        return provider


def decide_openrouter_selection(
    model_id: str,
    *,
    typed_input: str = "",
    current_provider: str = "",
    explicit_provider: str = "",
    selection_source: str = SELECTION_TYPED,
    user_providers: Optional[dict] = None,
    custom_providers: Optional[list] = None,
    subscription_providers: Optional[list[str]] = None,
    catalog_fn: Optional[Callable[[str], list[str]]] = None,
) -> OpenRouterSelectionDecision:
    """Decide a selection that resolved to OpenRouter model ``model_id``."""
    if selection_source != SELECTION_TYPED:
        return _ALLOW
    providers = (
        subscription_providers
        if subscription_providers is not None
        else configured_subscription_providers()
    )
    if not providers:
        return _ALLOW
    matches = subscription_matches(
        model_id,
        providers,
        user_providers=user_providers,
        custom_providers=custom_providers,
        catalog_fn=catalog_fn,
    )
    if not matches:
        return _ALLOW

    shown = typed_input.strip() or model_id
    if explicit_provider:
        options = "; ".join(
            f"`/model {native} --provider {prov}` ({_label(prov)})"
            for prov, native in matches.items()
        )
        return OpenRouterSelectionDecision(
            action="refuse",
            message=(
                f"`{shown}` is also on your subscription, so OpenRouter's "
                f"pay-per-token copy can only be chosen from the /model picker "
                f"(emergency use). Subscription route: {options}."
            ),
        )

    chosen = current_provider if current_provider in matches else next(iter(matches))
    native = matches[chosen]
    return OpenRouterSelectionDecision(
        action="reroute",
        provider=chosen,
        model=native,
        message=(
            f"`{shown}` would have used OpenRouter's pay-per-token copy; "
            f"routed to {_label(chosen)} (`{native}`) instead. OpenRouter copies "
            f"of subscription models are picker-only."
        ),
    )
