"""Slot allocator: atomic per-slot claim via flock, plus port derivation.

Every resource a slot needs (ports, profile dir) is derived from a single
integer `slot` index in `[0, pool_size)`. A process claims a slot by acquiring
an exclusive flock on a per-slot lockfile and holding the fd open for its
entire lifetime: the kernel releases the lock automatically when the holding
process exits, which is the entire stale-lock fix.
"""

from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LOCK_DIR = Path.home() / ".escarp" / "locks"
DEFAULT_PROFILE_DIR = Path.home() / ".escarp" / "profiles"


@dataclass(frozen=True)
class SlotPorts:
    frontend: int
    backend: int
    postgres: int
    cdp: int


def ports_for_slot(slot: int) -> SlotPorts:
    if slot < 0:
        raise ValueError(f"slot must be >= 0, got {slot}")
    return SlotPorts(
        frontend=3000 + slot * 10,
        backend=8000 + slot * 10,
        postgres=5432 + slot * 10,
        cdp=9222 + slot,
    )


def profile_dir_for_slot(slot: int, tier: str = "autonomous", root: Path | None = None) -> Path:
    root = root or DEFAULT_PROFILE_DIR
    return root / tier / f"slot-{slot}"


@dataclass
class SlotLease:
    slot: int
    ports: SlotPorts
    profile_dir: Path
    lock_path: Path
    _lock_fd: int

    def release(self) -> None:
        if self._lock_fd >= 0:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                object.__setattr__(self, "_lock_fd", -1)


class SlotBusy(Exception):
    """Raised when a slot is already claimed by another process."""


def claim_slot(
    slot: int,
    *,
    tier: str = "autonomous",
    lock_dir: Path | None = None,
    profile_root: Path | None = None,
) -> SlotLease:
    """Atomically claim a slot or raise SlotBusy.

    The returned lease must be kept alive (i.e. the SlotLease object held in
    scope) for the slot to remain claimed. When the process exits, the kernel
    releases the flock automatically.
    """
    lock_dir = lock_dir or DEFAULT_LOCK_DIR
    lock_dir.mkdir(parents=True, exist_ok=True)

    profile_dir = profile_dir_for_slot(slot, tier=tier, root=profile_root)
    profile_dir.parent.mkdir(parents=True, exist_ok=True)

    lock_path = lock_dir / f"slot-{slot}.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise SlotBusy(f"slot {slot} is already claimed (lockfile {lock_path})") from exc

    return SlotLease(
        slot=slot,
        ports=ports_for_slot(slot),
        profile_dir=profile_dir,
        lock_path=lock_path,
        _lock_fd=fd,
    )


def claim_any_slot(
    pool_size: int,
    *,
    tier: str = "autonomous",
    lock_dir: Path | None = None,
    profile_root: Path | None = None,
) -> SlotLease:
    """Claim the lowest available slot index in [0, pool_size)."""
    for slot in range(pool_size):
        try:
            return claim_slot(slot, tier=tier, lock_dir=lock_dir, profile_root=profile_root)
        except SlotBusy:
            continue
    raise SlotBusy(f"all {pool_size} slots are claimed")
