"""Best-effort accounting identity for bare custom transport labels.

This does not resolve credentials or change transports. Only a unique configured
endpoint identity may replace a bare label on a new accounting delta.
"""

from typing import Optional


def qualify_usage_provider(
    provider: Optional[str], base_url: Optional[str]
) -> Optional[str]:
    if provider != "custom" or not base_url:
        return provider
    try:
        from hermes_cli.config import (
            _normalize_custom_provider_entry,
            load_config_readonly,
            providers_dict_to_custom_providers,
        )
        from hermes_cli.providers import custom_provider_slug
        from hermes_cli.route_identity import normalize_route_base_url

        cfg = load_config_readonly()
        target = normalize_route_base_url(base_url)
        # Do not use the picker compatibility list here: its display-name
        # dedupe can collapse two keyed providers sharing one endpoint.
        entries = providers_dict_to_custom_providers(cfg.get("providers"))
        legacy = cfg.get("custom_providers")
        if isinstance(legacy, list):
            for raw in legacy:
                entry = _normalize_custom_provider_entry(raw)
                if entry is not None:
                    entries.append(entry)
        identities = {
            custom_provider_slug(entry["name"], entry.get("provider_key", ""))
            for entry in entries
            if entry and normalize_route_base_url(entry["base_url"]) == target
        }
        if len(identities) == 1:
            return next(iter(identities))
    except Exception:
        # Missing/malformed config must not lose usage or interrupt inference.
        pass
    return provider
