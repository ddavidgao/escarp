"""Broker daemon: discovers already-running browsers, brokers leases, runs reaper.

**Does NOT own chrome lifecycles.** Per the persistence contract,
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
import contextlib
import os
import signal
import sys
from pathlib import Path

from aiohttp import web

from escarp.broker.api import DEFAULT_PORT, bind_with_shift, build_app
from escarp.broker.browser import reset_browser_state
from escarp.broker.discovery import DiscoveredBrowser, discover_pool
from escarp.broker.lease import Broker, reaper_loop
from escarp.broker.pool import PoolController, register_browser
from escarp.broker.slots import SlotBusy, ports_for_slot
from escarp.pool_config import load_pool_config

DAEMON_PIDFILE = Path.home() / ".escarp" / "daemon.pid"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


def _format_table(browsers: list[DiscoveredBrowser]) -> str:
    rows = [f"{'slot':<6}{'cdp':<8}{'frontend':<10}{'backend':<10}cdp_ws_url"]
    for browser in browsers:
        ports = ports_for_slot(browser.slot)
        rows.append(
            f"{browser.slot:<6}"
            f"{browser.cdp_port:<8}"
            f"{ports.frontend:<10}"
            f"{ports.backend:<10}"
            f"{browser.cdp_ws_url}"
        )
    return "\n".join(rows)


async def _serve_http(broker: Broker, api_port: int, pool: PoolController) -> tuple[web.AppRunner, int]:

    app = build_app(broker, pool=pool)
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
    browsers: list[DiscoveredBrowser] = []

    async def reset_for_port(cdp_port: int) -> None:
        try:
            closed = await reset_browser_state(cdp_port)
            print(f"[reset] cdp_port={cdp_port} closed {closed} stale tab(s)", flush=True)
        except Exception as exc:
            print(f"[reset] cdp_port={cdp_port} failed: {exc}", file=sys.stderr, flush=True)

    broker = Broker(lease_ttl_s=lease_ttl_s, reset_fn=reset_for_port)
    controller = PoolController(broker, cdp_base=cdp_base_port)
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
        #    Same registration path as a hot `pool add`, so boot and live-add
        #    behave identically.
        for browser in discovered:
            try:
                reg = await register_browser(broker, browser, tier=controller.tier)
            except SlotBusy as exc:
                print(
                    f"[slot {browser.slot}] another broker holds the lock for this slot. {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            controller.adopt(browser.slot, reg.lease)
            browsers.append(browser)
            rec = reg.record
            print(
                f"[slot {browser.slot}] discovered  reset_closed={reg.stale_closed}"
                f"  os_window_id={rec.os_window_id}  owner_pid={rec.owner_pid}"
                + (f"  cua_app={rec.cua_app_bundle_id}" if rec.cua_app_bundle_id else "")
                + (f"  calib_warn={rec.calibration_note}" if rec.calibration_note else ""),
                flush=True,
            )

        if not browsers:
            print("no slots brokered. exiting.", file=sys.stderr, flush=True)
            return 1

        # 3. HTTP + reaper.
        runner, actual_port = await _serve_http(broker, api_port, controller)
        _write_pidfile(api_port=actual_port)
        reaper_task = asyncio.create_task(reaper_loop(broker, stop=stop_event))

        print()
        print(_format_table(browsers))
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
        controller.release_all()
        _clear_pidfile()
        # NOTE: intentionally do NOT touch chromes. They are infrastructure.


def _write_pidfile(*, api_port: int) -> None:
    """Record this daemon's pid (and the broker port it bound) so `escarp scale`
    can find it for a clean restart. Best-effort: a failure here never blocks
    the daemon from serving."""
    try:
        DAEMON_PIDFILE.parent.mkdir(parents=True, exist_ok=True)
        DAEMON_PIDFILE.write_text(f"{os.getpid()} {api_port}\n")
    except OSError as exc:
        print(f"[daemon] could not write pidfile {DAEMON_PIDFILE}: {exc}", file=sys.stderr)


def _clear_pidfile() -> None:
    with contextlib.suppress(OSError):
        DAEMON_PIDFILE.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    # Persisted pool config is the source of truth for the size; an explicit
    # ESCARP_POOL_SIZE env still overrides it for one-off runs.
    cfg = load_pool_config()
    pool_size = _env_int("ESCARP_POOL_SIZE", cfg.pool_size)
    if pool_size < 1:
        raise SystemExit(f"pool size must be >= 1, got {pool_size}")
    cdp_base_port = _env_int("ESCARP_CDP_BASE", cfg.cdp_base)
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
