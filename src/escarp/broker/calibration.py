"""Slot -> OS-window-id calibration.

The bridge problem: CDP exposes Chromium-internal windowId, the OS exposes
kCGWindowNumber, and there is no public API mapping between them. The robust
bridge per David's note is GEOMETRY: set each slot's window to a unique
rectangle via CDP Browser.setWindowBounds, enumerate CGWindowList for CfT
PIDs, find the window whose bounds match, cache the kCGWindowNumber.

This runs once per slot at daemon startup. The bound geometry is also useful
for humans — slots end up tiled, not stacked on top of each other.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass

import aiohttp


@dataclass(frozen=True)
class SlotGeometry:
    slot: int
    x: int
    y: int
    width: int
    height: int

    def as_bounds(self) -> tuple[float, float, float, float]:
        return (float(self.x), float(self.y), float(self.width), float(self.height))


def default_geometry(slot: int, *, cols: int = 2, width: int = 760, height: int = 580) -> SlotGeometry:
    """Tile slots in a grid starting from (40, 80). Per-slot offset keeps the
    geometry unique even for large pools."""
    col = slot % cols
    row = slot // cols
    return SlotGeometry(
        slot=slot,
        x=40 + col * (width + 20),
        y=80 + row * (height + 20),
        width=width,
        height=height,
    )


async def _cdp_browser_call(
    browser_ws_url: str,
    method: str,
    params: dict | None = None,
    *,
    timeout: float = 5.0,
) -> dict:
    """One-shot CDP call against a browser-level websocket. Returns the JSON
    response's `result` field (or raises if CDP returned an error)."""
    async with aiohttp.ClientSession() as session, session.ws_connect(browser_ws_url) as ws:
        await ws.send_json({"id": 1, "method": method, "params": params or {}})
        msg = await ws.receive(timeout=timeout)
        data = json.loads(msg.data)
        if "error" in data:
            raise RuntimeError(f"CDP {method} failed: {data['error']}")
        return data.get("result", {})


async def set_window_bounds(browser_ws_url: str, geom: SlotGeometry) -> tuple[int, str]:
    """Move/resize the browser's first window to `geom` via CDP.

    Returns (chromium_window_id, cdp_target_id) so the caller can record the
    Chromium-internal handles alongside the OS-level identity.
    """
    targets = await _cdp_browser_call(browser_ws_url, "Target.getTargets")
    page_target = next(
        (t for t in targets["targetInfos"] if t["type"] == "page"),
        None,
    )
    if page_target is None:
        raise RuntimeError("no page-type CDP target on this browser")
    cdp_target_id = page_target["targetId"]

    wfor = await _cdp_browser_call(
        browser_ws_url,
        "Browser.getWindowForTarget",
        {"targetId": cdp_target_id},
    )
    window_id = wfor["windowId"]

    await _cdp_browser_call(
        browser_ws_url,
        "Browser.setWindowBounds",
        {"windowId": window_id, "bounds": {"windowState": "normal"}},
    )
    await _cdp_browser_call(
        browser_ws_url,
        "Browser.setWindowBounds",
        {
            "windowId": window_id,
            "bounds": {
                "left": geom.x,
                "top": geom.y,
                "width": geom.width,
                "height": geom.height,
                "windowState": "normal",
            },
        },
    )
    return window_id, cdp_target_id


@dataclass(frozen=True)
class CalibrationResult:
    slot: int
    geom: SlotGeometry
    os_window_id: int | None
    owner_pid: int | None
    cdp_window_id: int | None = None
    cdp_target_id: str | None = None
    failed_reason: str | None = None

    def succeeded(self) -> bool:
        return self.os_window_id is not None


async def calibrate_slot(
    *,
    slot: int,
    browser_ws_url: str,
    initial_settle_s: float = 0.3,
    total_timeout_s: float = 4.0,
    poll_s: float = 0.2,
) -> CalibrationResult:
    """Set the slot's window to a unique known rectangle, then find the
    matching CG window. Polls CGWindowList for up to `total_timeout_s` to
    accommodate slow window-manager propagation (varies per macOS version,
    Stage Manager state, multi-monitor setups, etc.)
    """
    geom = default_geometry(slot)

    if sys.platform != "darwin":
        try:
            cdp_window_id, cdp_target_id = await set_window_bounds(browser_ws_url, geom)
        except Exception as exc:
            return CalibrationResult(
                slot=slot, geom=geom, os_window_id=None, owner_pid=None,
                failed_reason=f"setWindowBounds failed: {exc}",
            )
        return CalibrationResult(
            slot=slot, geom=geom, os_window_id=None, owner_pid=None,
            cdp_window_id=cdp_window_id, cdp_target_id=cdp_target_id,
            failed_reason="non-darwin: OS window identity not available",
        )

    try:
        cdp_window_id, cdp_target_id = await set_window_bounds(browser_ws_url, geom)
    except Exception as exc:
        return CalibrationResult(
            slot=slot, geom=geom, os_window_id=None, owner_pid=None,
            failed_reason=f"setWindowBounds failed: {exc}",
        )

    from escarp.broker.macos import enumerate_cft_windows, find_by_bounds

    await asyncio.sleep(initial_settle_s)
    deadline = asyncio.get_running_loop().time() + total_timeout_s
    last_seen_bounds: list[tuple[float, float, float, float]] = []
    while asyncio.get_running_loop().time() < deadline:
        match = find_by_bounds(geom.as_bounds())
        if match is not None:
            return CalibrationResult(
                slot=slot,
                geom=geom,
                os_window_id=match.window_number,
                owner_pid=match.owner_pid,
                cdp_window_id=cdp_window_id,
                cdp_target_id=cdp_target_id,
            )
        last_seen_bounds = [w.bounds for w in enumerate_cft_windows()]
        await asyncio.sleep(poll_s)

    return CalibrationResult(
        slot=slot, geom=geom, os_window_id=None, owner_pid=None,
        cdp_window_id=cdp_window_id, cdp_target_id=cdp_target_id,
        failed_reason=(
            f"no CfT window with bounds matching {geom.as_bounds()} after "
            f"{total_timeout_s}s. CG enumeration last saw: {last_seen_bounds}. "
            f"window manager may have ignored or rejected the bounds."
        ),
    )
