"""Minimal daemon entry point.

v2-MVP scope: stand up N persistent CfT windows, log their CDP URLs, hold
open until SIGINT/SIGTERM, then shut them down cleanly. No lease state, no
MCP shim yet — those land in Phase 3 and Phase 4.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

from escarp.broker.browser import (
    BrowserLaunchError,
    ManagedBrowser,
    find_cft_binary,
    launch_cft,
    shutdown,
)
from escarp.broker.slots import SlotBusy, SlotLease, claim_slot


DEFAULT_POOL_SIZE = 4


def _pool_size_from_env() -> int:
    raw = os.environ.get("ESCARP_POOL_SIZE")
    if raw is None:
        return DEFAULT_POOL_SIZE
    try:
        n = int(raw)
    except ValueError as exc:
        raise SystemExit(f"ESCARP_POOL_SIZE must be an integer, got {raw!r}") from exc
    if n < 1:
        raise SystemExit(f"ESCARP_POOL_SIZE must be >= 1, got {n}")
    return n


def _format_table(leases: list[SlotLease], browsers: list[ManagedBrowser]) -> str:
    rows = [f"{'slot':<6}{'cdp':<8}{'frontend':<10}{'backend':<10}{'pid':<8}cdp_ws_url"]
    for lease, browser in zip(leases, browsers, strict=True):
        rows.append(
            f"{lease.slot:<6}"
            f"{lease.ports.cdp:<8}"
            f"{lease.ports.frontend:<10}"
            f"{lease.ports.backend:<10}"
            f"{browser.pid:<8}"
            f"{browser.cdp_ws_url}"
        )
    return "\n".join(rows)


async def run_daemon(pool_size: int, cft_binary: Path) -> int:
    leases: list[SlotLease] = []
    browsers: list[ManagedBrowser] = []

    try:
        for slot in range(pool_size):
            try:
                lease = claim_slot(slot)
            except SlotBusy as exc:
                print(f"[slot {slot}] {exc} -- skipping", file=sys.stderr)
                continue
            try:
                browser = launch_cft(
                    slot=slot,
                    binary=cft_binary,
                    profile_dir=lease.profile_dir,
                    cdp_port=lease.ports.cdp,
                )
            except BrowserLaunchError as exc:
                print(f"[slot {slot}] launch failed: {exc}", file=sys.stderr)
                lease.release()
                continue
            leases.append(lease)
            browsers.append(browser)
            print(f"[slot {slot}] up  pid={browser.pid}  ws={browser.cdp_ws_url}")

        if not browsers:
            print("no browsers launched; exiting", file=sys.stderr)
            return 1

        print()
        print(_format_table(leases, browsers))
        print(f"\npool of {len(browsers)} ready. ctrl-c to shut down.\n")

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        await stop_event.wait()
        print("\nshutdown signal received, closing browsers...")
        return 0
    finally:
        for browser in browsers:
            shutdown(browser)
        for lease in leases:
            lease.release()
        print(f"shut down {len(browsers)} browser(s).")


def main(argv: list[str] | None = None) -> int:
    pool_size = _pool_size_from_env()
    cft = find_cft_binary()
    if cft is None:
        print(
            "Chrome for Testing binary not found.\n"
            "Set ESCARP_CFT_BINARY=/path/to/'Google Chrome for Testing' or run\n"
            "  npx @puppeteer/browsers install chrome@stable\n"
            "from the escarp repo root.",
            file=sys.stderr,
        )
        return 2
    print(f"using CfT binary: {cft}")
    print(f"pool size: {pool_size}\n")
    try:
        return asyncio.run(run_daemon(pool_size, cft))
    except KeyboardInterrupt:
        return 0
