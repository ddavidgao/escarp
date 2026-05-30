"""MCP server: three tools (escarp_status, escarp_acquire, escarp_release).

Transport: stdio. Register with Claude Code via:

    claude mcp add escarp -- uv --project /path/to/escarp run escarp-mcp

Register with Codex CLI:

    codex mcp add escarp uv --project /path/to/escarp run escarp-mcp

Once registered, the model never touches the broker directly -- it just calls
the three tools. The shim:
  - Translates tool calls into HTTP against the broker (ESCARP_BROKER_URL).
  - Holds the lease token in process memory; the model never sees it.
  - Runs a background heartbeat task while a lease is held, so a long-running
    model session can't time out mid-task. Heartbeat fires every (TTL / 3)
    seconds, well clear of the broker's reaper.
  - On shim shutdown (model disconnect / process exit), releases the lease so
    the slot returns to the pool. (Per V2_PLAN.md decision #5: disconnect
    frees the LEASE, not the browser.)
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import suppress
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

DEFAULT_BROKER_URL = "http://127.0.0.1:7878"
HEARTBEAT_DIVISOR = 3  # heartbeat every TTL/3 seconds


class LeaseState:
    """Process-wide singleton: which lease this shim is holding, if any."""

    def __init__(self) -> None:
        self.lease_token: str | None = None
        self.slot: int | None = None
        self.cdp_port: int | None = None
        self.ttl_s: float = 60.0
        self.heartbeat_task: asyncio.Task[None] | None = None

    def clear(self) -> None:
        if self.heartbeat_task is not None:
            self.heartbeat_task.cancel()
            self.heartbeat_task = None
        self.lease_token = None
        self.slot = None
        self.cdp_port = None


_LEASE = LeaseState()


def _broker_url() -> str:
    return os.environ.get("ESCARP_BROKER_URL", DEFAULT_BROKER_URL)


def _format_slot(s: dict[str, Any]) -> str:
    if s["state"] == "free":
        return (
            f"  slot {s['slot']}: FREE   "
            f"cdp_port={s['cdp_port']}  pid={s['pid']}  ws={s['cdp_ws_url']}"
        )
    return (
        f"  slot {s['slot']}: LEASED to {s['holder']!r}  "
        f"dev_port={s['dev_port']}  expires_at={s['expires_at']:.0f}  cdp_port={s['cdp_port']}"
    )


async def _heartbeat_loop(broker_url: str, lease_token: str, interval_s: float) -> None:
    """Background task: refresh the lease until it's released or the task is
    cancelled (process exit / disconnect)."""
    while True:
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            return
        try:
            async with httpx.AsyncClient(base_url=broker_url, timeout=5.0) as client:
                await client.post("/heartbeat", json={"lease_token": lease_token})
        except Exception as exc:
            print(f"[escarp-mcp] heartbeat failed: {exc}", file=sys.stderr)


mcp = FastMCP("escarp")


@mcp.tool()
async def escarp_status() -> str:
    """Show the broker's pool: which slots are free, which are leased, by whom,
    until when. Call this BEFORE acquire so you can see whether the pool has
    free slots and so you can reason about contention. Never blocks."""
    async with httpx.AsyncClient(base_url=_broker_url(), timeout=5.0) as client:
        resp = await client.get("/status")
    if resp.status_code != 200:
        return f"status request failed: {resp.status_code} {resp.text}"
    data = resp.json()
    lines = [
        f"pool_size={data['pool_size']}  lease_ttl={data['lease_ttl_s']}s",
        *[_format_slot(s) for s in data["slots"]],
    ]
    if _LEASE.lease_token is not None:
        lines.append(f"\nthis MCP session is currently holding slot {_LEASE.slot}.")
    return "\n".join(lines)


@mcp.tool()
async def escarp_acquire(
    holder: str,
    slot: int | None = None,
    dev_port: int | None = None,
) -> str:
    """Lease a browser slot from the broker. Returns a CDP websocket URL the
    caller can drive (via Playwright connect_over_cdp, chrome-devtools-mcp
    --browser-url, etc.).

    Auto-heartbeat keeps the lease alive while this MCP server is running.
    The lease is released automatically when this shim disconnects.

    Args:
        holder: Identifier for the agent (e.g. "claude-code-david-session-12").
                Shows up in escarp_status. Helps disambiguate concurrent agents.
        slot:   Optional. Specific slot index to acquire. Omit to get the
                lowest-numbered free slot.
        dev_port: Optional. Local dev server port the agent intends to test
                against (e.g. 3000). Stored on the lease for observability.
    """
    if _LEASE.lease_token is not None:
        return (
            f"this MCP session already holds slot {_LEASE.slot}. "
            f"Call escarp_release first if you want a different slot."
        )

    payload: dict[str, Any] = {"holder": holder}
    if slot is not None:
        payload["slot"] = slot
    if dev_port is not None:
        payload["dev_port"] = dev_port

    async with httpx.AsyncClient(base_url=_broker_url(), timeout=10.0) as client:
        resp = await client.post("/acquire", json=payload)
    if resp.status_code != 200:
        return f"acquire failed (HTTP {resp.status_code}): {resp.text}"

    data = resp.json()
    _LEASE.lease_token = data["lease_token"]
    _LEASE.slot = data["slot"]
    _LEASE.cdp_port = data["cdp_port"]

    # Refresh the TTL from broker's view of the world. Heartbeat at TTL/3.
    async with httpx.AsyncClient(base_url=_broker_url(), timeout=5.0) as client:
        status = (await client.get("/status")).json()
    ttl = float(status.get("lease_ttl_s", 60.0))
    _LEASE.ttl_s = ttl
    _LEASE.heartbeat_task = asyncio.create_task(
        _heartbeat_loop(_broker_url(), data["lease_token"], ttl / HEARTBEAT_DIVISOR)
    )

    return (
        f"acquired slot {data['slot']}\n"
        f"  cdp_ws_url: {data['cdp_ws_url']}\n"
        f"  cdp_port:   {data['cdp_port']}  (HTTP discovery at http://127.0.0.1:{data['cdp_port']}/json/list)\n"
        f"  dev_port:   {data['dev_port']}\n"
        f"  expires_at: {data['expires_at']:.0f}  (auto-heartbeat active, refreshes every {ttl/HEARTBEAT_DIVISOR:.0f}s)\n"
        f"\n"
        f"To drive this browser, point your CDP client at cdp_ws_url. The browser is\n"
        f"reset to a clean about:blank tab when you escarp_release()."
    )


@mcp.tool()
async def escarp_release() -> str:
    """Release the lease this MCP session is holding. The broker resets the
    browser (closes stray tabs, navigates back to about:blank) so the next
    holder gets a clean slate."""
    if _LEASE.lease_token is None:
        return "no active lease in this session."

    token = _LEASE.lease_token
    slot = _LEASE.slot
    async with httpx.AsyncClient(base_url=_broker_url(), timeout=10.0) as client:
        resp = await client.post("/release", json={"lease_token": token})
    _LEASE.clear()
    if resp.status_code != 200:
        return f"release returned HTTP {resp.status_code}: {resp.text}"
    return f"released slot {slot}. broker reset the browser to about:blank."


async def _release_on_exit() -> None:
    """Best-effort lease release if the process is shutting down with a
    lease still held."""
    if _LEASE.lease_token is None:
        return
    with suppress(Exception):
        async with httpx.AsyncClient(base_url=_broker_url(), timeout=3.0) as client:
            await client.post("/release", json={"lease_token": _LEASE.lease_token})
    _LEASE.clear()


def main() -> None:
    try:
        mcp.run()
    finally:
        # mcp.run() returns when the client disconnects. Free our lease so
        # the slot doesn't sit leased until the reaper notices.
        with suppress(Exception):
            asyncio.run(_release_on_exit())
