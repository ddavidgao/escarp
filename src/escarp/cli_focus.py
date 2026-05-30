"""`escarp focus <slot>` -- bring a slot's window to the OS foreground.

Thin CLI wrapper around `escarp.broker.focus.focus_slot`. Hits the broker's
/status endpoint to resolve the slot -> cdp_port mapping, then calls the
focus primitive. Idempotent.
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from escarp.broker.api import DEFAULT_PORT
from escarp.broker.focus import focus_slot


def main(slot: int) -> int:
    return asyncio.run(_run(slot))


async def _run(slot: int) -> int:
    broker_url = f"http://127.0.0.1:{DEFAULT_PORT}"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{broker_url}/status")
        resp.raise_for_status()
    except Exception as exc:
        print(
            f"could not reach broker at {broker_url}: {exc}\n"
            f"start the daemon first: escarp daemon",
            file=sys.stderr,
        )
        return 2

    data = resp.json()
    target = next((s for s in data["slots"] if s["slot"] == slot), None)
    if target is None:
        print(
            f"slot {slot} not in pool (size={data['pool_size']})", file=sys.stderr
        )
        return 2

    bounds = target.get("bounds")
    bounds_tuple = tuple(bounds) if bounds is not None else None
    result = await focus_slot(
        slot=slot,
        cdp_port=target["cdp_port"],
        cdp_ws_url=target["cdp_ws_url"],
        cg_window_number=target.get("os_window_id"),
        cg_window_owner_pid=target.get("owner_pid"),
        cg_window_bounds=bounds_tuple,  # type: ignore[arg-type]
    )

    print(f"slot {slot}  cdp_port={target['cdp_port']}  title='{result.title_set}'")
    print(f"  CDP bring_to_front:    {'ok' if result.cdp_bring_to_front else 'FAIL'}")
    if sys.platform == "darwin":
        print(f"  AX raise (exact win):  {'ok' if result.ax_raised else 'FAIL'}")
        print(f"  macOS app activate:    {'ok' if result.os_app_activated else 'FAIL'}")
        print(
            f"  verified frontmost:    "
            f"{'OK' if result.verified_frontmost else 'FAIL'}"
            + (
                f"  (slot's window = {result.cg_window_number}, "
                f"actually frontmost = {result.actually_frontmost_cg_window})"
                if not result.verified_frontmost and result.cg_window_number is not None
                else ""
            )
        )
    for note in result.notes:
        print(f"  note: {note}")
    print(
        f"\noverall: {'ok' if result.succeeded() else 'FAIL'}"
        + (" -- safe to hand off to Codex CUA" if result.succeeded() else " -- DO NOT claim CUA bridge for this slot")
    )
    return 0 if result.succeeded() else 1
