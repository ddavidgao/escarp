"""Launcher: idempotency + binary-missing handling.

These tests don't actually spawn chrome -- they mock `launch_cft` so we
verify the launcher's policy logic without paying the ~3s cold-start cost
per slot.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from escarp.broker.browser import BrowserLaunchError, ManagedBrowser
from escarp.broker.launcher import launch_pool


def _fake_browser(slot: int, cdp_port: int, tmp_path: Path) -> ManagedBrowser:
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 99000 + slot
    proc.poll = lambda: None
    return ManagedBrowser(
        slot=slot,
        cdp_port=cdp_port,
        cdp_ws_url=f"ws://127.0.0.1:{cdp_port}/devtools/browser/fake-{slot}",
        profile_dir=tmp_path / f"slot-{slot}",
        process=proc,
    )


@pytest.fixture
def fake_binary(tmp_path: Path) -> Path:
    binary = tmp_path / "fake-cft"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return binary


async def test_launch_pool_skips_slots_already_listening(
    fake_binary: Path, tmp_path: Path
) -> None:
    """If a cdp_port is already responding to /json/version, the launcher
    must NOT spawn another chrome on it. Idempotency is the contract."""

    async def fake_probe(cdp_port: int, *, timeout: float = 0.5) -> dict | None:
        # slot 0 (port 9322) is already up; slot 1 (port 9323) is not.
        if cdp_port == 9322:
            return {"Browser": "Chrome/149.0", "webSocketDebuggerUrl": "ws://..."}
        return None

    launched: list[int] = []

    def fake_launch_cft(*, slot: int, binary: Path, profile_dir: Path, cdp_port: int) -> ManagedBrowser:
        launched.append(slot)
        return _fake_browser(slot, cdp_port, tmp_path)

    with patch("escarp.broker.launcher.probe", side_effect=fake_probe), \
         patch("escarp.broker.launcher.launch_cft", side_effect=fake_launch_cft):
        rc = await launch_pool(pool_size=2, cdp_base_port=9322, cft_binary=fake_binary)

    assert rc == 0
    assert launched == [1]  # only slot 1 should have been launched


async def test_launch_pool_returns_nonzero_when_nothing_brokerable(
    fake_binary: Path,
) -> None:
    """If every slot fails to launch AND nothing was already up, launcher
    returns a nonzero exit code so a wrapping shell script can react."""

    async def fake_probe(cdp_port: int, *, timeout: float = 0.5) -> dict | None:
        return None  # nothing already up

    def fake_launch_cft(*, slot: int, binary: Path, profile_dir: Path, cdp_port: int) -> ManagedBrowser:
        raise BrowserLaunchError(f"simulated launch failure on slot {slot}")

    with patch("escarp.broker.launcher.probe", side_effect=fake_probe), \
         patch("escarp.broker.launcher.launch_cft", side_effect=fake_launch_cft):
        rc = await launch_pool(pool_size=2, cdp_base_port=9322, cft_binary=fake_binary)

    assert rc == 1


async def test_launch_pool_succeeds_if_all_slots_already_up(
    fake_binary: Path,
) -> None:
    """If every slot is already up before we even start, launcher reports
    success (idempotency: rerunning launch-pool is safe)."""

    async def fake_probe(cdp_port: int, *, timeout: float = 0.5) -> dict | None:
        return {"Browser": "Chrome/149.0", "webSocketDebuggerUrl": "ws://..."}

    launched: list[int] = []

    def fake_launch_cft(*, slot: int, binary: Path, profile_dir: Path, cdp_port: int) -> ManagedBrowser:
        launched.append(slot)  # should never be called
        return _fake_browser(slot, cdp_port, fake_binary.parent)

    with patch("escarp.broker.launcher.probe", side_effect=fake_probe), \
         patch("escarp.broker.launcher.launch_cft", side_effect=fake_launch_cft):
        rc = await launch_pool(pool_size=3, cdp_base_port=9322, cft_binary=fake_binary)

    assert rc == 0
    assert launched == []  # nothing launched, everything was already up
