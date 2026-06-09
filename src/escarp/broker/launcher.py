"""Dev-convenience launcher: spin up N detached Chrome for Testing processes.

Critically separated from the broker daemon. The library's persistence contract
is that **the broker does NOT own chrome lifecycles**. This launcher is one of
*many* ways to start chromes (launchd, systemd, docker, manual shell, etc.).
Once a chrome is up on its cdp_port, the broker's only relationship to it is
"talk to it over CDP."

This module exits after spawning. The chromes are detached (start_new_session)
and reparent to launchd/init -- killing the launcher process does NOT kill the
chromes. That's the whole point.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from escarp.broker.browser import (
    BrowserLaunchError,
    ManagedBrowser,
    find_cft_binary,
    launch_cft,
)
from escarp.broker.cua_apps import CuaAppError, CuaSlotApp, ensure_cua_slot_app
from escarp.broker.discovery import probe
from escarp.broker.slots import profile_dir_for_slot
from escarp.pool_config import DEFAULT_CDP_BASE, DEFAULT_POOL_SIZE


def _spawn_slot_chrome(
    *,
    slot: int,
    cft_binary: Path,
    cdp_port: int,
    tier: str,
    cua_apps: bool,
) -> tuple[ManagedBrowser, Path, CuaSlotApp | None]:
    """Launch one detached chrome for `slot`. In CUA mode, launch it from the
    per-slot app bundle so native CUA can target it. Returns (browser, profile,
    slot_app|None). Raises CuaAppError / BrowserLaunchError on failure."""
    profile = profile_dir_for_slot(slot, tier=tier)
    binary = cft_binary
    slot_app: CuaSlotApp | None = None
    if cua_apps:
        slot_app = ensure_cua_slot_app(slot=slot, cft_binary=cft_binary)
        binary = slot_app.binary_path
    browser = launch_cft(slot=slot, binary=binary, profile_dir=profile, cdp_port=cdp_port)
    return browser, profile, slot_app


async def ensure_slot_chrome(
    *,
    slot: int,
    cft_binary: Path,
    cdp_base_port: int = DEFAULT_CDP_BASE,
    tier: str = "autonomous",
    cua_apps: bool = False,
) -> bool:
    """Ensure a chrome is listening on the slot's cdp port, launching one if not.
    Returns True if it launched a new chrome, False if one was already up."""
    port = cdp_base_port + slot
    if await probe(port, timeout=0.5) is not None:
        return False
    _spawn_slot_chrome(slot=slot, cft_binary=cft_binary, cdp_port=port, tier=tier, cua_apps=cua_apps)
    return True


async def launch_pool(
    *,
    pool_size: int,
    cdp_base_port: int = DEFAULT_CDP_BASE,
    cft_binary: Path,
    tier: str = "autonomous",
    cua_apps: bool = False,
) -> int:
    """Spawn `pool_size` detached CfTs at cdp_base_port, cdp_base_port+1, ...

    Idempotent: if a cdp_port is already listening, skip that slot. So running
    `escarp launch-pool` twice doesn't double-launch.
    """
    print(f"using CfT binary: {cft_binary}")
    if cua_apps:
        print("native CUA mode: per-slot app bundles enabled")
    print(f"target: slots [0, {pool_size}) on cdp_ports {cdp_base_port}..{cdp_base_port + pool_size - 1}\n")

    launched = 0
    skipped = 0
    failed = 0
    for slot in range(pool_size):
        port = cdp_base_port + slot
        existing = await probe(port, timeout=0.5)
        if existing is not None:
            print(f"[slot {slot}] cdp_port {port} already listening ({existing.get('Browser','?')}) -- skipping")
            skipped += 1
            continue

        try:
            browser, profile, slot_app = _spawn_slot_chrome(
                slot=slot,
                cft_binary=cft_binary,
                cdp_port=port,
                tier=tier,
                cua_apps=cua_apps,
            )
        except CuaAppError as exc:
            print(f"[slot {slot}] CUA app setup failed: {exc}", file=sys.stderr)
            failed += 1
            continue
        except BrowserLaunchError as exc:
            print(f"[slot {slot}] launch failed: {exc}", file=sys.stderr)
            failed += 1
            continue

        cua_detail = (
            f"\n            cua_app={slot_app.app_path}"
            f"\n            cua_bundle_id={slot_app.bundle_id}"
            if slot_app
            else ""
        )
        print(
            f"[slot {slot}] launched  pid={browser.pid}  cdp_port={port}\n"
            f"            ws={browser.cdp_ws_url}\n"
            f"            profile={profile}"
            f"{cua_detail}"
        )
        launched += 1

    print(f"\nlaunched: {launched}   skipped (already up): {skipped}   failed: {failed}")
    if launched + skipped == 0:
        print("\nno browsers in the pool. nothing for `escarp daemon` to broker.", file=sys.stderr)
        return 1
    print("\nchromes are detached. they persist past this command's exit.")
    print("now run:  escarp daemon")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(prog="escarp launch-pool")
    parser.add_argument(
        "--pool-size",
        type=int,
        default=int(os.environ.get("ESCARP_POOL_SIZE", DEFAULT_POOL_SIZE)),
        help="how many CfT processes to spawn",
    )
    parser.add_argument(
        "--cdp-base-port",
        type=int,
        default=int(os.environ.get("ESCARP_CDP_BASE", DEFAULT_CDP_BASE)),
        help="cdp port for slot 0; slot N uses base+N",
    )
    parser.add_argument(
        "--cua-apps",
        action="store_true",
        help=(
            "macOS: launch each slot from a unique app bundle identity so native "
            "Codex CUA can target slots as separate apps"
        ),
    )
    args = parser.parse_args(argv)

    cft = find_cft_binary()
    if cft is None:
        print(
            "Chrome for Testing binary not found.\n"
            "Set ESCARP_CFT_BINARY=/path/to/'Google Chrome for Testing' or run\n"
            "  npx @puppeteer/browsers install chrome@stable",
            file=sys.stderr,
        )
        return 2

    rc = asyncio.run(
        launch_pool(
            pool_size=args.pool_size,
            cdp_base_port=args.cdp_base_port,
            cft_binary=cft,
            cua_apps=args.cua_apps,
        )
    )
    if rc == 0:
        # Record the launched shape so a later `escarp daemon` restart re-reads
        # this size instead of the default, and so `escarp scale` knows the
        # launch parameters (base port, cua mode) to reconcile against.
        from escarp.pool_config import PoolConfig, save_pool_config

        save_pool_config(
            PoolConfig(
                pool_size=args.pool_size,
                cdp_base=args.cdp_base_port,
                cua_apps=args.cua_apps,
                cft_binary=str(cft),
            )
        )
    return rc
