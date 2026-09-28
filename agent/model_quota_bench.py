"""Small, model-scoped quota bench sidecar for Claude-through-Meridian routes.

Account-wide limits use the normal credential pool. A model-family cap must not
mark every credential exhausted, so the quota job writes a separate atomic,
expiry-aware routing hint. Bad/missing state is treated as no signal.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

BENCH_FILE = "quota_model_benches.json"
_FAMILY = re.compile(r"^claude-([a-z0-9]+)-", re.IGNORECASE)


def _model_family(model: str) -> str | None:
    match = _FAMILY.match(str(model or ""))
    return match.group(1).lower() if match else None


def read_model_benches(home: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads((home / BENCH_FILE).read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("benches"), list):
            return []
        return data["benches"]
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def model_quota_benched_until(
    provider: str, model: str, *, base_url: str = "", home: Path | None = None
) -> float | None:
    """Return a future cliff only for the exact account and model family."""
    family = _model_family(model)
    if not family:
        return None
    pool_key = str(provider or "").strip().lower()
    if pool_key == "custom":
        try:
            from agent.credential_pool import get_custom_provider_pool_key
            pool_key = get_custom_provider_pool_key(base_url) or ""
        except Exception:
            return None
    if not pool_key.startswith("custom:meridian-"):
        return None
    if home is None:
        from hermes_cli.config import get_hermes_home
        home = Path(get_hermes_home())
    now = time.time()
    for row in read_model_benches(home):
        if not isinstance(row, dict):
            continue
        if row.get("pool_provider") != pool_key or row.get("family") != family:
            continue
        until = row.get("until")
        if isinstance(until, (int, float)) and not isinstance(until, bool) and math.isfinite(until) and until > now:
            return float(until)
    return None


def write_model_benches(home: Path, rows: list[dict[str, Any]]) -> None:
    """Replace the public routing hints atomically; no secrets are stored."""
    path = home / BENCH_FILE
    payload = json.dumps({"version": 1, "benches": rows}, sort_keys=True) + "\n"
    pending: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=home,
                                         prefix=".quota_model_benches-", delete=False) as stream:
            pending = stream.name
            os.chmod(pending, 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        if pending and os.path.exists(pending):
            os.unlink(pending)
