"""Lease broker: in-memory state machine for browser lease records.

Single source of truth, per V2_PLAN.md §4 locked decision #3. The model never
sees this state machine directly -- the MCP shim does, on the model's behalf.

Concurrency: all mutating methods hold an asyncio.Lock so concurrent acquire/
release/heartbeat calls (which can arrive from the HTTP layer in any order)
serialize cleanly. The reaper takes the same lock. Reset-on-release I/O runs
OUTSIDE the lock so a slow reset can't block acquires for other slots.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

# Default lease TTL: short enough that a dead agent doesn't park a slot for
# long, long enough that a healthy agent's heartbeat cadence (~TTL/3) doesn't
# spam the broker.
DEFAULT_LEASE_TTL_S = 60.0
DEFAULT_REAPER_INTERVAL_S = 2.0


class BrokerError(Exception):
    """Base for broker errors. Each subclass maps to a stable HTTP code."""


class PoolExhausted(BrokerError):
    pass


class UnknownSlot(BrokerError):
    pass


class SlotLeased(BrokerError):
    pass


class UnknownLease(BrokerError):
    pass


@dataclass
class LeaseRecord:
    slot: int
    cdp_port: int
    cdp_ws_url: str
    pid: int
    state: str = "free"
    tier: str = "autonomous"
    holder: str | None = None
    dev_port: int | None = None
    lease_token: str | None = None
    acquired_at: float | None = None
    expires_at: float | None = None
    last_heartbeat: float | None = None
    # Calibrated at daemon startup. The OS-level window identity is the load-
    # bearing primitive: kCGWindowNumber on macOS, equivalent on other OSes
    # eventually. Surfaced to API consumers as `os_window_id`. Populated only
    # when calibration could bind the slot's CDP window to a real OS window.
    os_window_id: int | None = None
    owner_pid: int | None = None
    bounds: tuple[float, float, float, float] | None = None
    cdp_window_id: int | None = None
    cdp_target_id: str | None = None
    calibration_note: str | None = None
    cua_app_bundle_id: str | None = None
    cua_app_path: str | None = None
    cua_app_name: str | None = None

    def to_public(
        self,
        *,
        lease_ttl_s: float,
        reaper_interval_s: float,
        now: float | None = None,
    ) -> dict[str, object]:
        d = asdict(self)
        # Don't leak the lease token in public snapshots (e.g. /status). The
        # token is returned only to the holder at acquire/heartbeat time.
        d.pop("lease_token", None)
        now = now if now is not None else time.time()
        d["suspected_stale"] = self._suspected_stale(lease_ttl_s=lease_ttl_s, now=now)
        d["available_after_s"] = self._available_after_s(
            reaper_interval_s=reaper_interval_s,
            now=now,
        )
        # Alias for agents that want an HTTP-style backoff field name.
        d["retry_after_s"] = d["available_after_s"]
        return d

    def _suspected_stale(self, *, lease_ttl_s: float, now: float) -> bool:
        if self.state != "leased" or self.last_heartbeat is None:
            return False
        return now - self.last_heartbeat > lease_ttl_s / 2

    def _available_after_s(self, *, reaper_interval_s: float, now: float) -> float:
        if self.state != "leased" or self.expires_at is None:
            return 0.0
        return max(0.0, self.expires_at - now) + reaper_interval_s


@dataclass
class _Reaped:
    slot: int
    holder: str
    reason: str


ResetFn = Callable[[int], Awaitable[None]]


class Broker:
    def __init__(
        self,
        *,
        lease_ttl_s: float = DEFAULT_LEASE_TTL_S,
        reaper_interval_s: float = DEFAULT_REAPER_INTERVAL_S,
        reset_fn: ResetFn | None = None,
    ) -> None:
        self._records: dict[int, LeaseRecord] = {}
        self._lock = asyncio.Lock()
        self._lease_ttl_s = lease_ttl_s
        self._reaper_interval_s = reaper_interval_s
        self._reaped_log: list[_Reaped] = []
        self._reset_fn = reset_fn

    @property
    def lease_ttl_s(self) -> float:
        return self._lease_ttl_s

    @property
    def reaper_interval_s(self) -> float:
        return self._reaper_interval_s

    def register(
        self,
        *,
        slot: int,
        cdp_port: int,
        cdp_ws_url: str,
        pid: int,
        tier: str = "autonomous",
        os_window_id: int | None = None,
        owner_pid: int | None = None,
        bounds: tuple[float, float, float, float] | None = None,
        cdp_window_id: int | None = None,
        cdp_target_id: str | None = None,
        calibration_note: str | None = None,
        cua_app_bundle_id: str | None = None,
        cua_app_path: str | None = None,
        cua_app_name: str | None = None,
    ) -> LeaseRecord:
        """Register a discovered browser into the pool."""
        rec = LeaseRecord(
            slot=slot,
            cdp_port=cdp_port,
            cdp_ws_url=cdp_ws_url,
            pid=pid,
            tier=tier,
            os_window_id=os_window_id,
            owner_pid=owner_pid,
            bounds=bounds,
            cdp_window_id=cdp_window_id,
            cdp_target_id=cdp_target_id,
            calibration_note=calibration_note,
            cua_app_bundle_id=cua_app_bundle_id,
            cua_app_path=cua_app_path,
            cua_app_name=cua_app_name,
        )
        self._records[slot] = rec
        return rec

    def snapshot(self) -> list[dict[str, object]]:
        now = time.time()
        return [
            rec.to_public(
                lease_ttl_s=self._lease_ttl_s,
                reaper_interval_s=self._reaper_interval_s,
                now=now,
            )
            for rec in sorted(self._records.values(), key=lambda r: r.slot)
        ]

    def pool_size(self) -> int:
        return len(self._records)

    def reaped_log(self) -> list[dict[str, object]]:
        return [asdict(r) for r in self._reaped_log[-50:]]

    async def acquire(
        self,
        *,
        holder: str,
        slot: int | None = None,
        dev_port: int | None = None,
    ) -> LeaseRecord:
        async with self._lock:
            if slot is None:
                target = self._lowest_free_slot()
                if target is None:
                    raise PoolExhausted("no free slots")
            else:
                if slot not in self._records:
                    raise UnknownSlot(f"slot {slot} not in pool (size={len(self._records)})")
                target = slot

            rec = self._records[target]
            if rec.state == "leased":
                raise SlotLeased(
                    f"slot {target} already leased to {rec.holder!r} (until {rec.expires_at})"
                )

            now = time.time()
            rec.state = "leased"
            rec.holder = holder
            rec.dev_port = dev_port
            rec.lease_token = secrets.token_urlsafe(16)
            rec.acquired_at = now
            rec.last_heartbeat = now
            rec.expires_at = now + self._lease_ttl_s
            return rec

    async def heartbeat(self, *, lease_token: str) -> LeaseRecord:
        async with self._lock:
            rec = self._find_by_token(lease_token)
            now = time.time()
            rec.last_heartbeat = now
            rec.expires_at = now + self._lease_ttl_s
            return rec

    async def release(self, *, lease_token: str) -> LeaseRecord:
        async with self._lock:
            rec = self._find_by_token(lease_token)
            cdp_port = rec.cdp_port
            self._reset(rec)
            # snapshot the now-freed state before exiting the lock so a racing
            # acquire on the same slot can't make our return value look leased
            snapshot = LeaseRecord(**asdict(rec))
        await self._invoke_reset(cdp_port, context="release")
        return snapshot

    async def reap(self, *, now: float | None = None) -> list[int]:
        """Free any leases whose expires_at has passed. Returns slot indices.

        State mutation happens under the lock; reset I/O fires after the lock is
        released so a slow chrome reset doesn't block other slots' acquires.
        """
        now = now if now is not None else time.time()
        async with self._lock:
            reaped_pairs: list[tuple[int, int]] = []  # (slot, cdp_port)
            for rec in self._records.values():
                if rec.state == "leased" and rec.expires_at is not None and rec.expires_at < now:
                    self._reaped_log.append(
                        _Reaped(
                            slot=rec.slot,
                            holder=rec.holder or "?",
                            reason=f"ttl expired at {rec.expires_at:.0f}",
                        )
                    )
                    reaped_pairs.append((rec.slot, rec.cdp_port))
                    self._reset(rec)
        for _slot, port in reaped_pairs:
            await self._invoke_reset(port, context="reap")
        return [slot for slot, _ in reaped_pairs]

    async def _invoke_reset(self, cdp_port: int, *, context: str) -> None:
        if self._reset_fn is None:
            return
        try:
            await self._reset_fn(cdp_port)
        except Exception as exc:
            print(
                f"[broker] reset_fn failed during {context} on cdp_port={cdp_port}: {exc}",
                file=sys.stderr,
            )

    def _lowest_free_slot(self) -> int | None:
        for slot in sorted(self._records.keys()):
            if self._records[slot].state == "free":
                return slot
        return None

    def _find_by_token(self, token: str) -> LeaseRecord:
        for rec in self._records.values():
            if rec.lease_token == token:
                return rec
        raise UnknownLease(f"no active lease for token {token[:8]}...")

    @staticmethod
    def _reset(rec: LeaseRecord) -> None:
        rec.state = "free"
        rec.holder = None
        rec.dev_port = None
        rec.lease_token = None
        rec.acquired_at = None
        rec.expires_at = None
        rec.last_heartbeat = None


async def reaper_loop(
    broker: Broker,
    *,
    interval_s: float | None = None,
    stop: asyncio.Event,
) -> None:
    """Background task: every `interval_s`, reap expired leases. Logs to stdout
    so the daemon operator can see reclamations as they happen.
    """
    interval_s = broker.reaper_interval_s if interval_s is None else interval_s
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            return
        except TimeoutError:
            pass
        reaped = await broker.reap()
        if reaped:
            print(f"[reaper] freed slots: {reaped}", flush=True)
