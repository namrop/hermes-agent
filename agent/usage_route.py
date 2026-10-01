"""Best-effort accounting identity for bare custom transport labels.

This does not resolve credentials or change transports. A bare ``custom``
label on a new accounting delta is replaced only by a configured identity for
the endpoint the call actually hit:

1. the provider the caller *requested* (``agent.requested_provider``), when
   it names a configured entry whose endpoint is that endpoint. This is what
   disambiguates several named providers sharing one server (the Acubens
   oMLX entries, keeper Discord #gateway msg 1555093184839942206);
2. otherwise the endpoint's identity when exactly one configured entry has
   that endpoint (keeper Discord 1553136245826265098).

Anything else stays ``custom``: unattributed beats misattributed. A requested
name for a different endpoint is ignored, so a call that fell back elsewhere
is never written under the name it was originally asked for.
"""

from typing import Optional


def qualify_usage_provider(
    provider: Optional[str],
    base_url: Optional[str],
    requested_provider: Optional[str] = None,
) -> Optional[str]:
    if provider != "custom" or not base_url:
        return provider
    try:
        from hermes_cli.config import (
            _normalize_custom_provider_entry,
            load_config_readonly,
            providers_dict_to_custom_providers,
        )
        from hermes_cli.providers import custom_provider_aliases, custom_provider_slug
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
        on_endpoint = [
            entry
            for entry in entries
            if entry and normalize_route_base_url(entry["base_url"]) == target
        ]
        requested = str(requested_provider or "").strip().lower()
        if requested and requested != "custom":
            named = {
                custom_provider_slug(entry["name"], entry.get("provider_key", ""))
                for entry in on_endpoint
                if requested
                in custom_provider_aliases(entry["name"], entry.get("provider_key", ""))
            }
            if len(named) == 1:
                return next(iter(named))
        identities = {
            custom_provider_slug(entry["name"], entry.get("provider_key", ""))
            for entry in on_endpoint
        }
        if len(identities) == 1:
            return next(iter(identities))
    except Exception:
        # Missing/malformed config must not lose usage or interrupt inference.
        pass
    return provider
