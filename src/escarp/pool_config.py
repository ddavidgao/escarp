"""Persisted pool configuration: the single canonical pool size.

The pool size is the one number that describes how many slots this machine
runs. It belongs on disk, not in an environment variable, because the chromes
outlive the daemon (they reparent to launchd) but a plain `escarp daemon`
restart would otherwise re-read the default and silently orphan every slot
above it -- the chrome stays alive, the lockfile stays on disk, but the broker
never registers it, so nobody can lease it.

Persisting the size closes that gap. The daemon's boot-time discovery reads the
*intended* size from here instead of from whatever environment launched it.

Precedence the daemon applies: ESCARP_POOL_SIZE env (explicit override) >
pool.json (persisted intent) > DEFAULT_POOL_SIZE.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_POOL_SIZE = 4
DEFAULT_CDP_BASE = 9222
DEFAULT_TIER = "autonomous"
DEFAULT_CONFIG_PATH = Path.home() / ".escarp" / "pool.json"


@dataclass
class PoolConfig:
    """Desired shape of the pool. `cua_apps` records whether the slots were
    launched from per-slot macOS app bundles, so a later `scale` relaunches the
    missing slots with the same identity mode instead of plain chromes."""

    pool_size: int = DEFAULT_POOL_SIZE
    cdp_base: int = DEFAULT_CDP_BASE
    tier: str = DEFAULT_TIER
    cua_apps: bool = False
    # Absolute path to the Chrome for Testing binary the pool was launched with.
    # Persisted so `scale` resolves it cwd-independently instead of relying on
    # find_cft_binary's cwd-relative search.
    cft_binary: str | None = None


def load_pool_config(path: Path | None = None) -> PoolConfig:
    """Load persisted config, or return defaults if the file is absent/corrupt.

    Never raises on a missing or malformed file: a fresh machine just gets the
    default 4-slot pool, which is the documented out-of-the-box behavior.
    """
    path = path or DEFAULT_CONFIG_PATH
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return PoolConfig()
    if not isinstance(data, dict):
        return PoolConfig()
    return PoolConfig(
        pool_size=int(data.get("pool_size", DEFAULT_POOL_SIZE)),
        cdp_base=int(data.get("cdp_base", DEFAULT_CDP_BASE)),
        tier=str(data.get("tier", DEFAULT_TIER)),
        cua_apps=bool(data.get("cua_apps", False)),
        cft_binary=(str(data["cft_binary"]) if data.get("cft_binary") else None),
    )


def save_pool_config(config: PoolConfig, path: Path | None = None) -> None:
    """Persist the desired pool config atomically (write-temp-then-rename)."""
    path = path or DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(config), indent=2) + "\n")
    tmp.replace(path)
