"""Hot pool membership: register/unregister slots against a running broker.

The daemon discovers its initial pool once at boot, but `escarp pool add/remove`
changes pool membership WITHOUT restarting the daemon. The load-bearing detail
is the slot lock (flock): a hot-added slot's SlotLease fd must be held for the
daemon's whole life, and a hot-removed slot's fd must be released. PoolController
owns those fds so the HTTP handlers can mutate membership while the daemon keeps
serving.

Chrome lifecycles stay outside this layer, per the persistence contract: add
registers an already-listening chrome (the CLI launches it first); remove only
unbrokers (the CLI kills the chrome afterward).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from escarp.broker.browser import reset_browser_state
from escarp.broker.calibration import calibrate_slot
from escarp.broker.cua_apps import existing_cua_slot_app
from escarp.broker.discovery import DiscoveredBrowser, probe
from escarp.broker.lease import Broker, LeaseRecord
from escarp.broker.slots import SlotBusy, SlotLease, claim_slot


class PoolError(Exception):
    """Base for hot-pool errors. Each maps to a stable HTTP code in the API."""


class NoBrowserOnPort(PoolError):
    pass


class SlotAlreadyInPool(PoolError):
    pass


@dataclass
class SlotRegistration:
    lease: SlotLease
    record: LeaseRecord
    stale_closed: int


async def register_browser(
    broker: Broker,
    browser: DiscoveredBrowser,
    *,
    tier: str = "autonomous",
) -> SlotRegistration:
    """Claim the slot lock, reset the browser, bind its identity, and register it
    into the broker. Raises SlotBusy if another process holds the slot lock.

    Shared by the daemon's boot discovery and PoolController.add_slot so both
    paths register a slot identically. In CUA app mode the slot is identified by
    its per-slot bundle and OS-window calibration is skipped; otherwise the slot
    is calibrated to an OS-window identity.
    """
    lease = claim_slot(browser.slot, tier=tier)  # raises SlotBusy
    try:
        stale_closed = await reset_browser_state(browser.cdp_port)
    except Exception:
        stale_closed = -1

    cua_app = existing_cua_slot_app(browser.slot)
    if cua_app is None:
        calib = await calibrate_slot(slot=browser.slot, browser_ws_url=browser.cdp_ws_url)
        record = broker.register(
            slot=browser.slot,
            cdp_port=browser.cdp_port,
            cdp_ws_url=browser.cdp_ws_url,
            pid=-1,
            os_window_id=calib.os_window_id,
            owner_pid=calib.owner_pid,
            bounds=calib.geom.as_bounds() if calib.succeeded() else None,
            cdp_window_id=calib.cdp_window_id,
            cdp_target_id=calib.cdp_target_id,
            calibration_note=calib.failed_reason,
        )
    else:
        record = broker.register(
            slot=browser.slot,
            cdp_port=browser.cdp_port,
            cdp_ws_url=browser.cdp_ws_url,
            pid=-1,
            calibration_note="CUA app identity mode; OS-window calibration skipped",
            cua_app_bundle_id=cua_app.bundle_id,
            cua_app_path=str(cua_app.app_path),
            cua_app_name=cua_app.display_name,
        )
    return SlotRegistration(lease=lease, record=record, stale_closed=stale_closed)


@dataclass
class PoolController:
    """Owns the slot lock fds for a running daemon so pool membership can change
    live. The broker holds the in-memory records; this holds the flocks."""

    broker: Broker
    cdp_base: int = 9222
    tier: str = "autonomous"
    locks: dict[int, SlotLease] = field(default_factory=dict)

    def adopt(self, slot: int, lease: SlotLease) -> None:
        """Record a lock fd claimed during boot discovery so the controller owns
        its lifetime (release on daemon shutdown or hot-remove)."""
        self.locks[slot] = lease

    async def add_slot(self, slot: int) -> LeaseRecord:
        """Hot-register the chrome already listening on this slot's cdp port."""
        if self.broker.has_slot(slot):
            raise SlotAlreadyInPool(f"slot {slot} is already in the pool")
        port = self.cdp_base + slot
        info = await probe(port, timeout=1.0)
        ws = info.get("webSocketDebuggerUrl") if info else None
        if info is None or not ws:
            raise NoBrowserOnPort(
                f"no chrome listening on cdp_port {port} for slot {slot}; launch it first"
            )
        browser = DiscoveredBrowser(
            slot=slot,
            cdp_port=port,
            cdp_ws_url=ws,
            browser_version=str(info.get("Browser", "")),
        )
        try:
            reg = await register_browser(self.broker, browser, tier=self.tier)
        except SlotBusy as exc:
            raise PoolError(f"slot {slot} lock is held by another process: {exc}") from exc
        self.locks[slot] = reg.lease
        return reg.record

    async def remove_slot(self, slot: int, *, force: bool = False) -> LeaseRecord:
        """Hot-unregister a slot and release its lock. Refuses a leased slot
        unless force=True. Does NOT touch the chrome (caller's job)."""
        rec = await self.broker.unregister(slot=slot, force=force)
        lease = self.locks.pop(slot, None)
        if lease is not None:
            lease.release()
        return rec

    def release_all(self) -> None:
        for lease in self.locks.values():
            lease.release()
        self.locks.clear()
