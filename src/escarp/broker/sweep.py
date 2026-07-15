"""Periodic process sweep: reap chromes that are dead weight, never live ones.

The persistence contract still stands -- the daemon never kills a healthy
chrome. What accumulates over days is garbage the port-based lifecycle can't
see, and this sweep collects exactly that:

  - orphans: escarp-signature chrome processes whose slot is not brokered
    (left over from an old, bigger pool, or a double-launch collision).
  - corpses: a brokered slot whose chrome has stopped answering CDP. The
    process is killed and the slot unbrokered so /status reflects reality;
    relaunching is still the CLI's job (`escarp pool add` / `escarp scale`).

Both require consecutive strikes before acting, so a chrome mid-registration
(`pool add` launches, then POSTs /pool/add) or a transiently stalled CDP is
never reaped. Leased slots are never swept: the lease TTL reaper frees the
lease first, then the next sweeps take over.
"""

from __future__ import annotations

import asyncio
import sys

from escarp.broker.discovery import probe
from escarp.broker.lease import Broker
from escarp.broker.pool import PoolController
from escarp.broker.procs import scan_escarp_chromes, terminate_pids

DEFAULT_SWEEP_INTERVAL_S = 60.0
ORPHAN_STRIKES = 2
DEAD_STRIKES = 3


class SweepState:
    """Strike counters carried across sweep passes."""

    def __init__(self) -> None:
        self.orphan_strikes: dict[int, int] = {}  # pid -> consecutive sightings
        self.dead_strikes: dict[int, int] = {}  # slot -> consecutive CDP failures


async def sweep_once(broker: Broker, controller: PoolController, state: SweepState) -> None:
    snapshot = broker.snapshot()
    brokered = {int(str(s["slot"])): str(s["state"]) for s in snapshot}
    cdp_ports = {int(str(s["slot"])): int(str(s["cdp_port"])) for s in snapshot}
    procs = await asyncio.to_thread(scan_escarp_chromes)

    # Orphans: escarp chromes the broker doesn't track.
    seen_orphans: set[int] = set()
    for proc in procs:
        if proc.slot is not None and proc.slot in brokered:
            continue
        seen_orphans.add(proc.pid)
        strikes = state.orphan_strikes.get(proc.pid, 0) + 1
        if strikes < ORPHAN_STRIKES:
            state.orphan_strikes[proc.pid] = strikes
            continue
        state.orphan_strikes.pop(proc.pid, None)
        await asyncio.to_thread(terminate_pids, [proc.pid])
        print(
            f"[sweep] reaped orphan chrome pid={proc.pid} slot={proc.slot} (not brokered)",
            flush=True,
        )
    for pid in list(state.orphan_strikes):
        if pid not in seen_orphans:
            del state.orphan_strikes[pid]

    # Corpses: brokered but CDP-unresponsive slots (skip leased ones).
    for slot, slot_state in brokered.items():
        if slot_state == "leased":
            state.dead_strikes.pop(slot, None)
            continue
        if await probe(cdp_ports[slot], timeout=2.0) is not None:
            state.dead_strikes.pop(slot, None)
            continue
        strikes = state.dead_strikes.get(slot, 0) + 1
        if strikes < DEAD_STRIKES:
            state.dead_strikes[slot] = strikes
            continue
        state.dead_strikes.pop(slot, None)
        pids = [p.pid for p in procs if p.slot == slot]
        if pids:
            await asyncio.to_thread(terminate_pids, pids)
        try:
            await controller.remove_slot(slot, force=True)
        except Exception as exc:
            print(f"[sweep] slot {slot}: unbroker failed: {exc}", file=sys.stderr, flush=True)
        print(
            f"[sweep] slot {slot}: chrome unresponsive for {DEAD_STRIKES} sweeps; "
            f"killed pid(s) {pids or 'none found'} and unbrokered the slot. "
            f"Relaunch with `escarp pool add {slot}`.",
            flush=True,
        )


async def sweep_loop(
    broker: Broker,
    controller: PoolController,
    *,
    interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
    stop: asyncio.Event,
) -> None:
    state = SweepState()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            return
        except TimeoutError:
            pass
        try:
            await sweep_once(broker, controller, state)
        except Exception as exc:
            print(f"[sweep] pass failed: {exc}", file=sys.stderr, flush=True)
