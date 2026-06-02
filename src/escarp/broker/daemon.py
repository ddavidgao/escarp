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
import contextlib
import os
import signal
import sys
from pathlib import Path

from aiohttp import web

from escarp.broker.api import DEFAULT_PORT, bind_with_shift, build_app
from escarp.broker.browser import reset_browser_state
from escarp.broker.calibration import calibrate_slot
from escarp.broker.cua_apps import existing_cua_slot_app
from escarp.broker.discovery import DiscoveredBrowser, discover_pool
from escarp.broker.lease import Broker, reaper_loop
from escarp.broker.slots import SlotBusy, SlotLease, claim_slot
from escarp.pool_config import DEFAULT_CDP_BASE, load_pool_config

DEFAULT_CDP_BASE_PORT = DEFAULT_CDP_BASE
DAEMON_PIDFILE = Path.home() / ".escarp" / "daemon.pid"


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

            try:
                stale_closed = await reset_browser_state(browser.cdp_port)
            except Exception as exc:
                stale_closed = -1
                print(f"[slot {browser.slot}] startup reset failed: {exc}", file=sys.stderr)

            cua_app = existing_cua_slot_app(browser.slot)
            if cua_app is None:
                calib = await calibrate_slot(slot=browser.slot, browser_ws_url=browser.cdp_ws_url)
                os_window_id = calib.os_window_id
                owner_pid = calib.owner_pid
                bounds = calib.geom.as_bounds() if calib.succeeded() else None
                cdp_window_id = calib.cdp_window_id
                cdp_target_id = calib.cdp_target_id
                calibration_note = calib.failed_reason
            else:
                # Native CUA targets the per-slot app bundle, not a
                # kCGWindowNumber. The old calibration path intentionally
                # resizes windows via CDP to bind CDP -> CGWindow; doing that
                # here is both unnecessary and visibly disruptive.
                os_window_id = None
                owner_pid = None
                bounds = None
                cdp_window_id = None
                cdp_target_id = None
                calibration_note = "CUA app identity mode; OS-window calibration skipped"
            broker.register(
                slot=browser.slot,
                cdp_port=browser.cdp_port,
                cdp_ws_url=browser.cdp_ws_url,
                pid=-1,
                os_window_id=os_window_id,
                owner_pid=owner_pid,
                bounds=bounds,
                cdp_window_id=cdp_window_id,
                cdp_target_id=cdp_target_id,
                calibration_note=calibration_note,
                cua_app_bundle_id=cua_app.bundle_id if cua_app else None,
                cua_app_path=str(cua_app.app_path) if cua_app else None,
                cua_app_name=cua_app.display_name if cua_app else None,
            )
            print(
                f"[slot {browser.slot}] discovered  reset_closed={stale_closed}"
                f"  os_window_id={os_window_id}  owner_pid={owner_pid}"
                + (f"  cua_app={cua_app.bundle_id}" if cua_app else "")
                + (f"  calib_warn={calibration_note}" if calibration_note else ""),
                flush=True,
            )

        if not browsers:
            print("no slots brokered. exiting.", file=sys.stderr, flush=True)
            return 1

        # 3. HTTP + reaper.
        runner, actual_port = await _serve_http(broker, api_port)
        _write_pidfile(api_port=actual_port)
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
