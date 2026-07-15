"""`escarp scale N` -- reconcile the running pool to N slots, then persist N.

The flow mirrors, as one safe command, the manual dance of "launch the missing
chromes, stop the daemon, relaunch it at the new size":

  1. Measure reality: probe the cdp ports to see which slots are actually live,
     AND scan the process table for escarp-signature chromes, so crashed/hung
     chromes (which the port scan cannot see) are still reconciled.
  2. Reconcile to N: reap CDP-dead chromes, idempotently launch the slots below
     N that are missing, terminate the live slots at or above N.
  3. Persist N to pool.json so a future daemon restart re-reads it.
  4. Clean-restart the broker daemon so it brokers exactly [0, N).
     N=0 is a full teardown: every chrome is killed and the daemon is stopped.

The broker never owns chrome lifecycles, so the killing lives here -- the same
lifecycle-owning layer as `escarp launch-pool`. The broker side only ever
"removes" a slot by being restarted at the smaller size.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from escarp.broker.discovery import probe
from escarp.broker.launcher import launch_pool
from escarp.broker.procs import ChromeProc, scan_escarp_chromes, terminate_pids
from escarp.pool_config import (
    DEFAULT_CONFIG_PATH,
    PoolConfig,
    load_pool_config,
    save_pool_config,
)
from escarp.slot_ops import (
    broker_status,
    find_broker_port,
    find_daemon_pid,
    remove_slot_data,
    resolve_cft_binary,
    resolve_cua_apps,
    stop_daemon,
    terminate_slot_chromes,
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


def _dead_chrome_procs(procs: list[ChromeProc], *, live: set[int]) -> list[ChromeProc]:
    """Escarp chromes the port scan can't vouch for: the slot has no CDP
    listener (crashed or hung chrome, or one parked beyond the scan window),
    or the slot index didn't parse from the profile path at all. These are
    invisible to the port-based lifecycle and are what accumulates for days."""
    return [p for p in procs if p.slot is None or p.slot not in live]


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
    pid = find_daemon_pid()
    if pid is not None:
        print(f"stopping daemon pid {pid} (chromes stay alive) ...")
        if not stop_daemon(pid):
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
    parser.add_argument(
        "size", type=int, help="target number of slots (0 = tear down the pool and stop the daemon)"
    )
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
    if target < 0:
        print("pool size must be >= 0 (0 tears the pool down and stops the daemon).", file=sys.stderr)
        return 2

    cfg = load_pool_config()
    cdp_base = args.cdp_base if args.cdp_base is not None else cfg.cdp_base
    cua_apps = resolve_cua_apps(args.cua_apps, cfg)

    # 1. Measure reality, two ways. The port scan finds healthy chromes; the
    #    process scan finds every escarp-signature chrome regardless of CDP
    #    state, so crashed/hung chromes (invisible to the port scan) and slots
    #    beyond the scan window still get reconciled.
    upper = max(target, cfg.pool_size) + SCAN_HEADROOM
    live = asyncio.run(_scan_live_slots(cdp_base, upper))
    procs = scan_escarp_chromes()
    to_launch = sorted(set(range(target)) - live)
    to_remove = sorted(s for s in live if s >= target)
    dead_procs = _dead_chrome_procs(procs, live=live)

    print(f"current live slots: {sorted(live) if live else '(none)'}")
    print(
        f"target {target} (cdp_base={cdp_base}, cua_apps={cua_apps})  ->  "
        f"launch {to_launch or '[]'}, remove {to_remove or '[]'}"
    )
    if dead_procs:
        print(
            "stale escarp chromes to reap (no cdp listener): "
            + ", ".join(f"pid={p.pid} slot={p.slot}" for p in dead_procs)
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

    # 3. Reap CDP-dead chromes BEFORE launching: a hung chrome squatting on a
    #    keeper slot's profile would collide with its relaunch, and one on a
    #    removed slot would survive the port-based termination below.
    if dead_procs:
        reaped = terminate_pids([p.pid for p in dead_procs])
        print(f"reaped {len(reaped)} stale chrome(s): pids {reaped}")
        if not args.keep_data:
            for slot in {p.slot for p in dead_procs if p.slot is not None and p.slot >= target}:
                remove_slot_data(slot, cua_apps=cua_apps)

    # 4. Scale up: idempotent launch fills the gaps below target.
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

    # 5. Scale down: terminate the excess chromes (the daemon never would).
    for slot in to_remove:
        port = cdp_base + slot
        killed = terminate_slot_chromes(slot, port, cua_apps=cua_apps)
        print(f"[slot {slot}] chrome on cdp_port {port}: {'terminated' if killed else 'not running'}")
        if not args.keep_data:
            remove_slot_data(slot, cua_apps=cua_apps)

    # 6. Persist the new canonical size (and the binary path we resolved, so the
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

    # 7. Target 0 is a full teardown: nothing left to broker, so stop the
    #    daemon instead of restarting it into a guaranteed boot failure.
    if target == 0:
        pid = find_daemon_pid()
        if pid is None:
            print("no running daemon found; pool is fully torn down.")
            return 0
        print(f"stopping daemon pid {pid} (pool torn down) ...")
        if not stop_daemon(pid):
            print(f"warning: daemon pid {pid} did not exit cleanly", file=sys.stderr)
            return 1
        print("daemon stopped. Run `escarp scale N` to recreate the pool.")
        return 0

    # 8. Clean-restart the daemon so it brokers exactly [0, target).
    if args.no_restart:
        print("--no-restart: pool reconciled and size persisted; restart the daemon to apply.")
        return 0
    return _restart_daemon()
