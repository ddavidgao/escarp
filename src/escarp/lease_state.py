"""Local lease-state file at ~/.escarp/leases.json.

The broker is the single source of truth for lease state (per V2_PLAN.md
decision #3), but it identifies leases by token, which the user can't be
expected to remember between `acquire` and `release`. This module keeps a
local cache of "leases I (this machine) recently acquired" so commands like
`escarp release --mine` can resolve to the right tokens.

It is NOT a parallel state machine -- the broker can invalidate our local
entries at any time (TTL expiry, reaper). We treat the file as a hint and
let the broker reject stale entries.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_PATH = Path(os.environ.get("ESCARP_LEASES_FILE", str(Path.home() / ".escarp" / "leases.json")))


@dataclass
class LocalLease:
    slot: int
    holder: str
    lease_token: str
    cdp_port: int
    cdp_ws_url: str
    acquired_at: float


def _read(path: Path) -> list[LocalLease]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text() or "[]")
    except json.JSONDecodeError:
        return []
    out: list[LocalLease] = []
    for item in raw:
        try:
            out.append(LocalLease(**item))
        except TypeError:
            continue
    return out


def _write(path: Path, leases: list[LocalLease]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(l) for l in leases], indent=2))


def all_leases(path: Path = DEFAULT_PATH) -> list[LocalLease]:
    return _read(path)


def add(lease: LocalLease, path: Path = DEFAULT_PATH) -> None:
    leases = _read(path)
    # If we already have an entry for this slot, replace it. The broker has
    # one lease per slot, so locally we should too.
    leases = [l for l in leases if l.slot != lease.slot]
    leases.append(lease)
    _write(path, leases)


def remove_by_token(token: str, path: Path = DEFAULT_PATH) -> bool:
    leases = _read(path)
    keep = [l for l in leases if l.lease_token != token]
    if len(keep) == len(leases):
        return False
    _write(path, keep)
    return True


def remove_by_slot(slot: int, path: Path = DEFAULT_PATH) -> LocalLease | None:
    leases = _read(path)
    target = next((l for l in leases if l.slot == slot), None)
    if target is None:
        return None
    _write(path, [l for l in leases if l.slot != slot])
    return target


def find_by_slot(slot: int, path: Path = DEFAULT_PATH) -> LocalLease | None:
    return next((l for l in _read(path) if l.slot == slot), None)


def find_by_holder(holder: str, path: Path = DEFAULT_PATH) -> list[LocalLease]:
    return [l for l in _read(path) if l.holder == holder]


def make(record_from_broker: dict, holder: str | None = None) -> LocalLease:
    """Build a LocalLease from a broker /acquire response."""
    return LocalLease(
        slot=record_from_broker["slot"],
        holder=record_from_broker.get("holder") or holder or "",
        lease_token=record_from_broker["lease_token"],
        cdp_port=record_from_broker["cdp_port"],
        cdp_ws_url=record_from_broker["cdp_ws_url"],
        acquired_at=record_from_broker.get("acquired_at") or time.time(),
    )
