"""Unit tests for slots: port derivation + flock semantics."""

from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path

import pytest

from escarp.broker.slots import (
    SlotBusy,
    claim_any_slot,
    claim_slot,
    ports_for_slot,
)


def test_ports_for_slot_zero() -> None:
    ports = ports_for_slot(0)
    assert ports.frontend == 3000
    assert ports.backend == 8000
    assert ports.postgres == 5432
    assert ports.cdp == 9222


def test_ports_for_slot_three() -> None:
    ports = ports_for_slot(3)
    assert ports.frontend == 3030
    assert ports.backend == 8030
    assert ports.postgres == 5462
    assert ports.cdp == 9225


def test_ports_negative_rejected() -> None:
    with pytest.raises(ValueError):
        ports_for_slot(-1)


def test_claim_slot_creates_lock_file(tmp_path: Path) -> None:
    lease = claim_slot(2, lock_dir=tmp_path / "locks", profile_root=tmp_path / "profiles")
    try:
        assert lease.slot == 2
        assert lease.ports.cdp == 9224
        assert lease.lock_path.exists()
        assert lease.profile_dir == tmp_path / "profiles" / "autonomous" / "slot-2"
    finally:
        lease.release()


def test_claim_slot_twice_in_same_process_busy(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    profile_root = tmp_path / "profiles"
    first = claim_slot(0, lock_dir=lock_dir, profile_root=profile_root)
    try:
        with pytest.raises(SlotBusy):
            claim_slot(0, lock_dir=lock_dir, profile_root=profile_root)
    finally:
        first.release()


def _hold_slot(lock_dir: str, profile_root: str, ready_fd: int) -> None:
    """Subprocess helper: claim slot 0, signal parent, sleep until killed."""
    import time

    lease = claim_slot(0, lock_dir=Path(lock_dir), profile_root=Path(profile_root))
    os.write(ready_fd, b"R")
    try:
        while True:
            time.sleep(1)
    finally:
        lease.release()


def test_slot_lock_holds_across_processes(tmp_path: Path) -> None:
    lock_dir = str(tmp_path / "locks")
    profile_root = str(tmp_path / "profiles")
    read_fd, write_fd = os.pipe()
    ctx = mp.get_context("fork")
    proc = ctx.Process(target=_hold_slot, args=(lock_dir, profile_root, write_fd))
    proc.start()
    os.close(write_fd)
    try:
        # wait for child to signal it has the lock
        assert os.read(read_fd, 1) == b"R"
        with pytest.raises(SlotBusy):
            claim_slot(0, lock_dir=Path(lock_dir), profile_root=Path(profile_root))
    finally:
        proc.terminate()
        proc.join(timeout=2)
        os.close(read_fd)


def test_slot_lock_released_when_process_dies(tmp_path: Path) -> None:
    """kernel auto-releases flock on process exit -- that's the entire stale-lock fix."""
    lock_dir = str(tmp_path / "locks")
    profile_root = str(tmp_path / "profiles")
    read_fd, write_fd = os.pipe()
    ctx = mp.get_context("fork")
    proc = ctx.Process(target=_hold_slot, args=(lock_dir, profile_root, write_fd))
    proc.start()
    os.close(write_fd)
    try:
        assert os.read(read_fd, 1) == b"R"
        proc.terminate()
        proc.join(timeout=2)
        # now slot 0 should be reclaimable by us
        lease = claim_slot(0, lock_dir=Path(lock_dir), profile_root=Path(profile_root))
        lease.release()
    finally:
        os.close(read_fd)


def test_claim_any_slot_walks_pool(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    profile_root = tmp_path / "profiles"
    s0 = claim_slot(0, lock_dir=lock_dir, profile_root=profile_root)
    s1 = claim_slot(1, lock_dir=lock_dir, profile_root=profile_root)
    try:
        any_lease = claim_any_slot(pool_size=4, lock_dir=lock_dir, profile_root=profile_root)
        assert any_lease.slot == 2
        any_lease.release()
    finally:
        s0.release()
        s1.release()


def test_claim_any_slot_exhausted(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    profile_root = tmp_path / "profiles"
    held = [
        claim_slot(s, lock_dir=lock_dir, profile_root=profile_root) for s in range(2)
    ]
    try:
        with pytest.raises(SlotBusy):
            claim_any_slot(pool_size=2, lock_dir=lock_dir, profile_root=profile_root)
    finally:
        for lease in held:
            lease.release()
