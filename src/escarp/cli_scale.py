"""`escarp scale N` -- reconcile the running pool to N slots, then persist N.

The flow mirrors, as one safe command, the manual dance of "launch the missing
chromes, stop the daemon, relaunch it at the new size":

  1. Measure reality: probe the cdp ports to see which slots are actually live.
  2. Reconcile to N: idempotently launch the slots below N that are missing;
     terminate the live slots at or above N.
  3. Persist N to pool.json so a future daemon restart re-reads it.
  4. Clean-restart the broker daemon so it brokers exactly [0, N).

The broker never owns chrome lifecycles, so the killing lives here -- the same
lifecycle-owning layer as `escarp launch-pool`. The broker side only ever
"removes" a slot by being restarted at the smaller size.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from escarp.broker.api import DEFAULT_PORT
from escarp.broker.daemon import DAEMON_PIDFILE
from escarp.broker.discovery import probe
from escarp.broker.launcher import launch_pool
from escarp.pool_config import (
    DEFAULT_CONFIG_PATH,
    PoolConfig,
    load_pool_config,
    save_pool_config,
)
from escarp.slot_ops import (
    broker_status,
    find_broker_port,
    pids_listening_on,
    remove_slot_data,
    resolve_cft_binary,
    resolve_cua_apps,
    terminate_chrome_on_port,
)

# How far above the target we scan when measuring reality, so a scale-down can
# discover (and remove) slots that sit above the persisted size.
SCAN_HEADROOM = 16


# --------------------------------------------------------------------------- #
# Reality measurement                                                         #
# --------------------------------------------------------------------------- #
async def _scan_live_slots(cdp_base: int, upper: int, *, timeout: float = 0.3) -> set[int]:
    """Probe cdp_base+slot for slot in [0, upper). Return the live slot set."""

    async def one(slot: int) -> int | None:
        info = await probe(cdp_base + slot, timeout=timeout)
        return slot if info else None

    results = await asyncio.gather(*(one(s) for s in range(upper)))
    return {s for s in results if s is not None}


# --------------------------------------------------------------------------- #
# Broker queries                                                              #
# --------------------------------------------------------------------------- #
def _leased_slots_among(slots: list[int]) -> list[int]:
    """Of `slots`, which are currently leased (so removing them would yank a
    slot out from under a working agent)?"""
    port = find_broker_port()
    if port is None:
        return []
    status = broker_status(port)
    if not status:
        return []
    leased = {s["slot"] for s in status.get("slots", []) if s.get("state") == "leased"}
    return sorted(set(slots) & leased)


# --------------------------------------------------------------------------- #
# Process management                                                          #
# --------------------------------------------------------------------------- #
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pidfile() -> tuple[int, int] | None:
    try:
        parts = DAEMON_PIDFILE.read_text().split()
        return int(parts[0]), (int(parts[1]) if len(parts) > 1 else DEFAULT_PORT)
    except (FileNotFoundError, ValueError, IndexError):
        return None


def _stop_daemon(pid: int, *, grace: float = 8.0) -> bool:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.15)
    return not _pid_alive(pid)


def _start_daemon() -> int:
    escarp_bin = shutil.which("escarp")
    cmd = [escarp_bin, "daemon"] if escarp_bin else [sys.executable, "-m", "escarp.cli", "daemon"]
    log_path = Path.home() / ".escarp" / "daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as log:
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    return proc.pid


def _wait_for_pool(*, timeout: float = 20.0) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        port = find_broker_port()
        if port is not None:
            status = broker_status(port)
            if status and status.get("slots"):
                return status
        time.sleep(0.3)
    return None


# --------------------------------------------------------------------------- #
# Restart                                                                     #
# --------------------------------------------------------------------------- #
def _restart_daemon() -> int:
    info = _read_pidfile()
    pid = info[0] if info and _pid_alive(info[0]) else None
    if pid is None:
        port = find_broker_port()
        if port is not None:
            pids = pids_listening_on(port)
            pid = pids[0] if pids else None

    if pid is not None:
        print(f"stopping daemon pid {pid} (chromes stay alive) ...")
        if not _stop_daemon(pid):
            print(f"warning: daemon pid {pid} did not exit cleanly; continuing", file=sys.stderr)
    else:
        print("no running daemon found; starting a fresh one.")

    new_pid = _start_daemon()
    print(f"started daemon pid {new_pid}; waiting for the broker to come up ...")
    status = _wait_for_pool()
    if status is None:
        print(
            "daemon did not report a ready pool in time. Check ~/.escarp/daemon.log",
            file=sys.stderr,
        )
        return 1
    brokered = len(status.get("slots", []))
    print(f"daemon up: {brokered} slot(s) brokered (pool_size={status.get('pool_size')}).")
    return 0


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="escarp scale",
        description="resize the pool to N slots and persist N as the new default.",
    )
    parser.add_argument("size", type=int, help="target number of slots")
    parser.add_argument("--cdp-base", type=int, default=None, help="cdp port for slot 0 (default: persisted)")
    parser.add_argument(
        "--cua-apps",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="launch new slots from per-slot app bundles (default: persisted / autodetected)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="remove a slot even if it is currently leased to an agent",
    )
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="on scale-down, keep removed slots' profiles and cua app bundles",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="reconcile chromes and persist N, but do not restart the daemon",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    target = args.size
    if target < 1:
        print("pool size must be >= 1 (stop the daemon to tear the pool down fully).", file=sys.stderr)
        return 2

    cfg = load_pool_config()
    cdp_base = args.cdp_base if args.cdp_base is not None else cfg.cdp_base
    cua_apps = resolve_cua_apps(args.cua_apps, cfg)

    # 1. Measure reality.
    upper = max(target, cfg.pool_size) + SCAN_HEADROOM
    live = asyncio.run(_scan_live_slots(cdp_base, upper))
    to_launch = sorted(set(range(target)) - live)
    to_remove = sorted(s for s in live if s >= target)

    print(f"current live slots: {sorted(live) if live else '(none)'}")
    print(
        f"target {target} (cdp_base={cdp_base}, cua_apps={cua_apps})  ->  "
        f"launch {to_launch or '[]'}, remove {to_remove or '[]'}"
    )

    # 2. Safety: never yank a leased slot without --force.
    if to_remove:
        leased = _leased_slots_among(to_remove)
        if leased and not args.force:
            print(
                f"refusing to remove leased slot(s) {leased}: an agent is holding them. "
                f"Re-run with --force to override.",
                file=sys.stderr,
            )
            return 3

    if args.dry_run:
        print("dry-run: no changes made.")
        return 0

    # 3. Scale up: idempotent launch fills the gaps below target.
    cft = resolve_cft_binary(cfg)
    if to_launch:
        if cft is None:
            print(
                "Chrome for Testing binary not found; cannot launch new slots.\n"
                "Set ESCARP_CFT_BINARY or run: npx @puppeteer/browsers install chrome@stable",
                file=sys.stderr,
            )
            return 2
        asyncio.run(
            launch_pool(
                pool_size=target,
                cdp_base_port=cdp_base,
                cft_binary=cft,
                cua_apps=cua_apps,
            )
        )

    # 4. Scale down: terminate the excess chromes (the daemon never would).
    for slot in to_remove:
        port = cdp_base + slot
        killed = terminate_chrome_on_port(port)
        print(f"[slot {slot}] chrome on cdp_port {port}: {'terminated' if killed else 'not running'}")
        if not args.keep_data:
            remove_slot_data(slot, cua_apps=cua_apps)

    # 5. Persist the new canonical size (and the binary path we resolved, so the
    #    next scale finds it cwd-independently).
    cft_to_persist = str(cft) if cft is not None else cfg.cft_binary
    save_pool_config(
        PoolConfig(
            pool_size=target,
            cdp_base=cdp_base,
            tier=cfg.tier,
            cua_apps=cua_apps,
            cft_binary=cft_to_persist,
        )
    )
    print(f"persisted pool_size={target} to {DEFAULT_CONFIG_PATH}")

    # 6. Clean-restart the daemon so it brokers exactly [0, target).
    if args.no_restart:
        print("--no-restart: pool reconciled and size persisted; restart the daemon to apply.")
        return 0
    return _restart_daemon()
