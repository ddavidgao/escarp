"""Broker daemon: launches the browser pool, exposes the HTTP API, runs the reaper.

End-to-end shape for v2-MVP:

    [user] $ escarp daemon
        |
        +-- claim N slot locks (flock)
        +-- launch N detached CfT processes (one per slot)
        +-- Broker registers each browser
        +-- aiohttp server starts on 127.0.0.1:7878
        +-- reaper task starts (sweeps every 2s)
        +-- ^C -> cancel reaper, close server, SIGTERM all browsers, release locks
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

from aiohttp import web

from escarp.broker.api import DEFAULT_PORT, bind_with_shift, build_app
from escarp.broker.browser import (
    BrowserLaunchError,
    ManagedBrowser,
    find_cft_binary,
    launch_cft,
    reset_browser_state,
    shutdown,
)
from escarp.broker.lease import Broker, reaper_loop
from escarp.broker.slots import SlotBusy, SlotLease, claim_slot


DEFAULT_POOL_SIZE = 4


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


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


async def _serve_http(broker: Broker, api_port: int) -> tuple[web.AppRunner, int]:
    app = build_app(broker)
    runner = web.AppRunner(app)
    await runner.setup()
    sock, actual_port = bind_with_shift("127.0.0.1", api_port)
    site = web.SockSite(runner, sock)
    await site.start()
    return runner, actual_port


async def run_daemon(pool_size: int, cft_binary: Path, api_port: int, lease_ttl_s: float) -> int:
    leases: list[SlotLease] = []
    browsers: list[ManagedBrowser] = []

    async def reset_for_port(cdp_port: int) -> None:
        closed = await reset_browser_state(cdp_port)
        print(f"[reset] cdp_port={cdp_port} closed {closed} stale tab(s)", flush=True)

    broker = Broker(lease_ttl_s=lease_ttl_s, reset_fn=reset_for_port)
    runner: web.AppRunner | None = None
    reaper_task: asyncio.Task[None] | None = None
    stop_event = asyncio.Event()

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
            broker.register(
                slot=slot,
                cdp_port=browser.cdp_port,
                cdp_ws_url=browser.cdp_ws_url,
                pid=browser.pid,
            )
            # Reset on startup so any stale session-restored tabs from prior runs
            # don't leak into this daemon's pool. The lease boundary is also
            # reset (see Broker.release / Broker.reap), but startup is the only
            # time the prior holder is "the previous daemon."
            try:
                stale_closed = await reset_browser_state(browser.cdp_port)
            except Exception as exc:
                stale_closed = -1
                print(f"[slot {slot}] reset on startup failed: {exc}", file=sys.stderr)
            print(
                f"[slot {slot}] up  pid={browser.pid}  ws={browser.cdp_ws_url}"
                f"  (reset closed {stale_closed} stale tab(s))"
            )

        if not browsers:
            print("no browsers launched; exiting", file=sys.stderr)
            return 1

        runner, actual_port = await _serve_http(broker, api_port)
        reaper_task = asyncio.create_task(reaper_loop(broker, stop=stop_event))

        print()
        print(_format_table(leases, browsers))
        print()
        print(f"broker http api:  http://127.0.0.1:{actual_port}")
        print(f"lease ttl:        {lease_ttl_s}s   reaper interval: 2s")
        print(f"try: curl http://127.0.0.1:{actual_port}/status | jq")
        print(f"\npool of {len(browsers)} ready. ctrl-c to shut down.\n")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        await stop_event.wait()
        print("\nshutdown signal received, closing broker + browsers...")
        return 0
    finally:
        stop_event.set()
        if reaper_task is not None:
            try:
                await asyncio.wait_for(reaper_task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                reaper_task.cancel()
        if runner is not None:
            await runner.cleanup()
        for browser in browsers:
            shutdown(browser)
        for lease in leases:
            lease.release()
        print(f"shut down {len(browsers)} browser(s).")


def main(argv: list[str] | None = None) -> int:
    pool_size = _env_int("ESCARP_POOL_SIZE", DEFAULT_POOL_SIZE)
    if pool_size < 1:
        raise SystemExit(f"ESCARP_POOL_SIZE must be >= 1, got {pool_size}")
    api_port = _env_int("ESCARP_API_PORT", DEFAULT_PORT)
    lease_ttl_s = float(os.environ.get("ESCARP_LEASE_TTL_S", "60"))

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
        return asyncio.run(run_daemon(pool_size, cft, api_port, lease_ttl_s))
    except KeyboardInterrupt:
        return 0
