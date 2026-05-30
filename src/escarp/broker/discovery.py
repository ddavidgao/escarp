"""Discover already-running Chrome for Testing instances by cdp_port.

Escarp v2's persistence contract: the broker does NOT own chrome lifecycles.
Chromes are infrastructure that exists independently of escarp. The daemon's
job is to discover what's listening on the per-slot cdp_ports and broker
leases against those, full stop. If a chrome dies, that's somebody else's
problem (or a launchd/systemd auto-restart's job).

How to start chromes:
  - `escarp launch-pool` (dev convenience, one-shot)
  - launchd/systemd unit
  - manual: `chrome-for-testing --remote-debugging-port=9222 --user-data-dir=...`
  - docker
  - whatever you want -- as long as cdp_ports 9222..9222+N-1 are listening.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class DiscoveredBrowser:
    slot: int
    cdp_port: int
    cdp_ws_url: str
    browser_version: str


async def probe(cdp_port: int, *, timeout: float = 2.0) -> dict | None:
    """GET /json/version on cdp_port. Returns the json dict or None if nothing
    is listening / response unparseable. Per Phase 0 findings, /json/version
    is the right discovery primitive on CfT 149+."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"http://127.0.0.1:{cdp_port}/json/version")
            resp.raise_for_status()
            return resp.json()
    except (httpx.HTTPError, ValueError):
        return None


async def discover_pool(
    *,
    pool_size: int,
    cdp_base_port: int = 9222,
    wait_for_each: float = 0.0,
) -> tuple[list[DiscoveredBrowser], list[int]]:
    """Walk slots [0, pool_size). For each slot, probe cdp_base_port+slot.

    Returns (discovered, missing_slots). `missing_slots` is the list of slot
    indices whose cdp_port wasn't responding -- the caller decides what to do
    about that (warn, retry, error out).
    """
    discovered: list[DiscoveredBrowser] = []
    missing: list[int] = []
    for slot in range(pool_size):
        port = cdp_base_port + slot
        info = await _probe_with_retry(port, wait_for_each=wait_for_each)
        if info is None:
            missing.append(slot)
            continue
        ws = info.get("webSocketDebuggerUrl")
        if not ws:
            missing.append(slot)
            continue
        discovered.append(
            DiscoveredBrowser(
                slot=slot,
                cdp_port=port,
                cdp_ws_url=ws,
                browser_version=str(info.get("Browser", "")),
            )
        )
    return discovered, missing


async def _probe_with_retry(cdp_port: int, *, wait_for_each: float) -> dict | None:
    """Best-effort: probe once; if missing and wait_for_each > 0, keep trying
    up to that many seconds. Useful right after `escarp launch-pool` since
    chrome can take a couple seconds to bind its CDP port."""
    info = await probe(cdp_port)
    if info is not None or wait_for_each <= 0:
        return info
    deadline = asyncio.get_running_loop().time() + wait_for_each
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.2)
        info = await probe(cdp_port)
        if info is not None:
            return info
    return None
