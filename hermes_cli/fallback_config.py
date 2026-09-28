"""Helpers for reading the effective fallback provider chain from config."""

from __future__ import annotations

from typing import Any


def _normalized_base_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/")


def resolve_entry_api_key(entry: dict[str, Any] | None) -> str | None:
    """API key for one fallback entry: inline ``api_key``, else ``key_env``.

    Mirrors the custom-provider convention (``key_env`` names the env var
    holding the key; ``api_key_env`` accepted as an alias). Returns None when
    neither yields a non-empty value, letting ``resolve_runtime_provider``
    fall through to the provider's standard credential resolution.

    ``key_env`` is resolved through ``agent.secret_scope.get_secret`` rather
    than a raw ``os.getenv`` — in a multiplexed gateway a bare env read would
    ignore the active profile's scope and can return another profile's
    credential. ``get_secret`` already implements the right fallback: it
    reads ``os.environ`` when there's no active multiplexed scope (matching
    prior single-profile behavior), and fails closed only when multiplexing
    is active with no scope installed.
    """
    if not isinstance(entry, dict):
        return None
    inline = str(entry.get("api_key") or "").strip()
    if inline:
        return inline
    key_env = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    if key_env:
        from agent.secret_scope import get_secret

        return (get_secret(key_env) or "").strip() or None
    return None


def _iter_fallback_entries(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        candidates = [raw]
    elif isinstance(raw, list):
        candidates = raw
    else:
        return []

    entries: list[dict[str, Any]] = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if not provider or not model:
            continue

        normalized = dict(entry)
        normalized["provider"] = provider
        normalized["model"] = model

        base_url = _normalized_base_url(entry.get("base_url"))
        if base_url:
            normalized["base_url"] = base_url

        entries.append(normalized)
    return entries


def _entry_identity(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("provider") or "").strip().lower(),
        str(entry.get("model") or "").strip().lower(),
        _normalized_base_url(entry.get("base_url")).lower(),
    )


def get_fallback_chain(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the effective fallback chain merged across old and new config keys.

    ``fallback_providers`` remains the primary source of truth and keeps its
    order. Legacy ``fallback_model`` entries are appended afterwards unless
    they target the same provider/model/base_url route as an earlier entry.
    The returned list always contains fresh dict copies.
    """

    config = config or {}
    chain: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for key in ("fallback_providers", "fallback_model"):
        for entry in _iter_fallback_entries(config.get(key)):
            identity = _entry_identity(entry)
            if identity in seen:
                continue
            seen.add(identity)
            chain.append(entry)

    return chain


# ---------------------------------------------------------------------------
# Resolve-time fallback eligibility
#
# Provider resolution happens BEFORE any model client exists, so a failure here
# is invisible to the conversation-loop fallback machinery (which only engages
# once a client is streaming). Both the cron scheduler and agent init need the
# same answer to "is this resolve failure worth walking the chain for?", so the
# predicate lives here — next to ``get_fallback_chain``, which is what the
# callers reach for immediately afterwards.
# ---------------------------------------------------------------------------

_TRANSIENT_TRANSPORT_EXC_NAMES = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "NetworkError",
        "TimeoutException",
        "ClientConnectorError",
        "ClientConnectorDNSError",
        "ServerTimeoutError",
        "ClientOSError",
    }
)

_TRANSIENT_MESSAGE_NEEDLES = (
    "nodename nor servname",
    "name or service not known",
    "temporary failure in name resolution",
    "failed to resolve",
    "connection refused",
    "network is unreachable",
    "timed out",
    "timeout",
)

_TRANSIENT_OSERROR_MESSAGE_NEEDLES = (
    "nodename nor servname",
    "name or service not known",
    "temporary failure in name resolution",
    "network is unreachable",
)


def is_transient_provider_resolve_error(exc: BaseException) -> bool:
    """True when primary provider resolution failed for a transient network reason.

    Provider resolution refreshes OAuth credentials and performs discovery
    before the agent loop starts. A short DNS outage (Cloudflare WARP / macOS
    resolver blip) surfaces as an httpx/httpcore ConnectError or a raw OSError
    errno 8 ("nodename nor servname provided") and must be eligible for
    ``fallback_providers`` the same way ``AuthError`` already is — otherwise a
    healthy rung never gets tried and the whole turn dies before the first
    model call.
    """
    import errno as _errno
    import socket as _socket

    # Walk the cause chain; callers wrap raw transport errors.
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        name = type(cur).__name__
        module = type(cur).__module__ or ""
        msg = str(cur).lower()

        # Explicit transport classes from httpx/httpcore/aiohttp.
        if name in _TRANSIENT_TRANSPORT_EXC_NAMES:
            return True
        if "httpx" in module or "httpcore" in module or "aiohttp" in module:
            if any(needle in msg for needle in _TRANSIENT_MESSAGE_NEEDLES):
                return True
        if isinstance(cur, OSError):
            # Platform-safe classification: socket.gaierror carries getaddrinfo
            # codes (EAI_*), plain OSError carries errno; compare each against
            # its own constant namespace.
            if isinstance(cur, _socket.gaierror):
                _eai_transient = {
                    getattr(_socket, _n)
                    for _n in ("EAI_NONAME", "EAI_AGAIN", "EAI_FAIL", "EAI_NODATA")
                    if hasattr(_socket, _n)
                }
                if cur.errno in _eai_transient:
                    return True
            else:
                err_no = getattr(cur, "errno", None)
                if err_no in {
                    _errno.ECONNREFUSED,
                    _errno.ECONNRESET,
                    _errno.EHOSTUNREACH,
                    _errno.ENETUNREACH,
                    _errno.ENETDOWN,
                    _errno.ETIMEDOUT,
                    _errno.EAGAIN,
                }:
                    return True
            if any(needle in msg for needle in _TRANSIENT_OSERROR_MESSAGE_NEEDLES):
                return True
        # Bare RuntimeError/Exception that already carries the DNS text
        # (format_runtime_provider_error sometimes surfaces the raw message).
        if "nodename nor servname" in msg or "name or service not known" in msg:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


# AuthError codes that describe a MISCONFIGURATION rather than a credential
# that is merely absent, expired, or benched. Silently walking the chain for
# these would hide a typo in config.yaml behind a working-but-unrequested
# provider — the operator would never learn their configured provider name is
# wrong. Everything else (missing token, refresh failed, quota/pool exhausted)
# is a credential-state problem that another rung legitimately solves.
_CONFIG_ERROR_AUTH_CODES = frozenset({"invalid_provider", "no_provider_configured"})


def classify_provider_resolve_error(exc: BaseException) -> str | None:
    """Classify a provider-resolution failure for fallback purposes.

    Returns ``"auth"`` for credential failures (missing, expired, or — the case
    that motivated this — a credential pool whose every entry is benched or
    exhausted), ``"transient network"`` for recoverable transport failures, and
    ``None`` when the error is neither and must therefore propagate untouched.

    ``None`` is the important return: a genuine resolver bug or a misconfigured
    provider name must surface, not get silently rerouted onto a different
    provider.
    """
    try:
        from hermes_cli.auth import AuthError
    except Exception:  # pragma: no cover - defensive, auth import is cheap
        AuthError = ()  # type: ignore[assignment]

    if AuthError and isinstance(exc, AuthError):  # type: ignore[arg-type]
        if getattr(exc, "code", None) in _CONFIG_ERROR_AUTH_CODES:
            return None
        return "auth"
    if is_transient_provider_resolve_error(exc):
        return "transient network"
    return None
