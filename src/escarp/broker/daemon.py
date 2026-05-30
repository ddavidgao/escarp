"""Broker daemon: discovers already-running browsers, brokers leases, runs reaper.

**Does NOT own chrome lifecycles.** Per V2_PLAN.md's persistence contract,
chromes are infrastructure that exists independently. To start the chromes,
run `escarp launch-pool` (or launchd, systemd, docker, manual shell, whatever).
This daemon's only relationship to a chrome is "discover via /json/version,
talk to it over CDP, never kill it."

End-to-end shape:

    [user] $ escarp launch-pool          # one-shot, exits, chromes persist
    [user] $ escarp daemon               # discovers chromes, brokers leases
        |
        +-- claim N slot locks (flock)   <- "I am the broker for this pool"
        +-- discover each slot via /json/version
        +-- Broker.register() each discovered browser
        +-- reset each browser to about:blank (state hygiene at boot)
        +-- aiohttp server starts on 127.0.0.1:7878
        +-- reaper task starts (sweeps every 2s)
        +-- ^C -> cancel reaper, close server, release locks. CHROMES STAY ALIVE.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys

from aiohttp import web

from escarp.broker.api import DEFAULT_PORT, bind_with_shift, build_app
from escarp.broker.browser import reset_browser_state
from escarp.broker.discovery import DiscoveredBrowser, discover_pool
from escarp.broker.lease import Broker, reaper_loop
from escarp.broker.slots import SlotBusy, SlotLease, claim_slot

DEFAULT_POOL_SIZE = 4
DEFAULT_CDP_BASE_PORT = 9222


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


def _format_table(leases: list[SlotLease], browsers: list[DiscoveredBrowser]) -> str:
    rows = [f"{'slot':<6}{'cdp':<8}{'frontend':<10}{'backend':<10}cdp_ws_url"]
    for lease, browser in zip(leases, browsers, strict=True):
        rows.append(
            f"{lease.slot:<6}"
            f"{lease.ports.cdp:<8}"
            f"{lease.ports.frontend:<10}"
            f"{lease.ports.backend:<10}"
            f"{browser.cdp_ws_url}"
        )
    return "\n".join(rows)


async def _serve_http(broker: Broker, api_port: int) -> tuple[web.AppRunner, int]:

    app = build_app(broker)
    runner = web.AppRunner(app)
    await runner.setup()
    sock, actual_port = bind_with_shift("127.0.0.1", api_port)
    site = web.SockSite(runner, sock)
    await site.start()
    return runner, actual_port


async def run_daemon(
    *,
    pool_size: int,
    cdp_base_port: int,
    api_port: int,
    lease_ttl_s: float,
    discovery_wait_s: float,
) -> int:
    leases: list[SlotLease] = []
    browsers: list[DiscoveredBrowser] = []

    async def reset_for_port(cdp_port: int) -> None:
        try:
            closed = await reset_browser_state(cdp_port)
            print(f"[reset] cdp_port={cdp_port} closed {closed} stale tab(s)", flush=True)
        except Exception as exc:
            print(f"[reset] cdp_port={cdp_port} failed: {exc}", file=sys.stderr, flush=True)

    broker = Broker(lease_ttl_s=lease_ttl_s, reset_fn=reset_for_port)
    runner: web.AppRunner | None = None
    reaper_task: asyncio.Task[None] | None = None
    stop_event = asyncio.Event()

    try:
        # 1. Discover whatever's already running.
        print(
            f"discovering pool: slots [0, {pool_size}) on cdp_ports "
            f"{cdp_base_port}..{cdp_base_port + pool_size - 1}",
            flush=True,
        )
        discovered, missing = await discover_pool(
            pool_size=pool_size,
            cdp_base_port=cdp_base_port,
            wait_for_each=discovery_wait_s,
        )
        for slot in missing:
            print(
                f"[slot {slot}] no chrome on cdp_port {cdp_base_port + slot}. "
                f"Run `escarp launch-pool` (or start a chrome there) first.",
                file=sys.stderr,
                flush=True,
            )
        if not discovered:
            print("nothing to broker. exiting.", file=sys.stderr, flush=True)
            return 1

        # 2. Claim slot locks for whatever's discovered; register into the broker.
        for browser in discovered:
            try:
                lease = claim_slot(browser.slot)
            except SlotBusy as exc:
                print(
                    f"[slot {browser.slot}] another broker holds the lock for this slot. {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            leases.append(lease)
            browsers.append(browser)
            broker.register(
                slot=browser.slot,
                cdp_port=browser.cdp_port,
                cdp_ws_url=browser.cdp_ws_url,
                pid=-1,  # we don't own this process
            )
            try:
                stale_closed = await reset_browser_state(browser.cdp_port)
            except Exception as exc:
                stale_closed = -1
                print(f"[slot {browser.slot}] startup reset failed: {exc}", file=sys.stderr)
            print(
                f"[slot {browser.slot}] discovered  ws={browser.cdp_ws_url}"
                f"  (reset closed {stale_closed} stale tab(s))",
                flush=True,
            )

        if not browsers:
            print("no slots brokered. exiting.", file=sys.stderr, flush=True)
            return 1

        # 3. HTTP + reaper.
        runner, actual_port = await _serve_http(broker, api_port)
        reaper_task = asyncio.create_task(reaper_loop(broker, stop=stop_event))

        print()
        print(_format_table(leases, browsers))
        print()
        print(f"broker http api:  http://127.0.0.1:{actual_port}")
        print(f"lease ttl:        {lease_ttl_s}s   reaper interval: 2s")
        print(f"try: curl http://127.0.0.1:{actual_port}/status | jq")
        print(
            f"\npool of {len(browsers)} brokered. chromes are NOT owned by this daemon "
            f"and will survive ctrl-c.",
            flush=True,
        )
        print("ctrl-c to release the slot locks and stop brokering.\n", flush=True)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        await stop_event.wait()
        print("\nshutdown signal received. releasing slot locks; chromes left alive.")
        return 0
    finally:
        stop_event.set()
        if reaper_task is not None:
            try:
                await asyncio.wait_for(reaper_task, timeout=3.0)
            except (TimeoutError, asyncio.CancelledError):
                reaper_task.cancel()
        if runner is not None:
            await runner.cleanup()
        for lease in leases:
            lease.release()
        # NOTE: intentionally do NOT touch chromes. They are infrastructure.


def main(argv: list[str] | None = None) -> int:
    pool_size = _env_int("ESCARP_POOL_SIZE", DEFAULT_POOL_SIZE)
    if pool_size < 1:
        raise SystemExit(f"ESCARP_POOL_SIZE must be >= 1, got {pool_size}")
    cdp_base_port = _env_int("ESCARP_CDP_BASE", DEFAULT_CDP_BASE_PORT)
    api_port = _env_int("ESCARP_API_PORT", DEFAULT_PORT)
    lease_ttl_s = float(os.environ.get("ESCARP_LEASE_TTL_S", "60"))
    discovery_wait_s = float(os.environ.get("ESCARP_DISCOVERY_WAIT_S", "0"))

    try:
        return asyncio.run(
            run_daemon(
                pool_size=pool_size,
                cdp_base_port=cdp_base_port,
                api_port=api_port,
                lease_ttl_s=lease_ttl_s,
                discovery_wait_s=discovery_wait_s,
            )
        )
    except KeyboardInterrupt:
        return 0
