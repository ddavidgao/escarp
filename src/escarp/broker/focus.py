"""Bring a slot's CfT window to the OS foreground -- and PROVE it.

v1.1 first cut relied on title-based AX targeting and reported success based
on which subroutines didn't error. David caught the failure mode: CDP +
osascript-app-activate can report green while the actually-frontmost OS
window is a different slot's window. This rewrite uses CGWindowNumber
identity and adversarial post-focus verification.

Strategy:
  1. Look up the slot's pre-calibrated kCGWindowNumber (from broker state).
  2. AX raise the exact window via geometric match (bounds we calibrated).
  3. macOS app activate (brings the app forward over other apps).
  4. POST-CHECK: re-enumerate CGWindowList and confirm our window is now the
     frontmost CfT window. If not, return FAIL with the actually-frontmost
     window's number for debugging.

If the slot has not been calibrated (e.g. daemon was started before this
code shipped), we report `not_calibrated` honestly and fall back to a
CDP-only Page.bringToFront. The caller (and the user) gets to see that the
CUA bridge is not guaranteed.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

import httpx

SLOT_TITLE_PREFIX = "escarp-slot-"


def slot_title(slot: int) -> str:
    """Human-readable label for the slot's tab/window title.

    Kept as a label only -- title is NOT used as the authority for focus.
    See research/cua_targeting.md for why.
    """
    return f"{SLOT_TITLE_PREFIX}{slot}"


@dataclass
class FocusResult:
    slot: int
    cdp_port: int
    title_set: str
    cdp_bring_to_front: bool = False
    os_app_activated: bool = False
    ax_raised: bool = False
    verified_frontmost: bool = False
    cg_window_number: int | None = None
    actually_frontmost_cg_window: int | None = None
    notes: list[str] = field(default_factory=list)

    def succeeded(self) -> bool:
        if sys.platform != "darwin":
            # On non-darwin, CDP is the only layer we have.
            return self.cdp_bring_to_front
        # On darwin we DEMAND the verified-frontmost check passed. CDP alone
        # is not enough for the CUA bridge claim.
        return self.verified_frontmost


async def focus_slot(
    *,
    slot: int,
    cdp_port: int,
    cdp_ws_url: str,
    cg_window_number: int | None = None,
    cg_window_owner_pid: int | None = None,
    cg_window_bounds: tuple[float, float, float, float] | None = None,
    app_name: str = "Google Chrome for Testing",
) -> FocusResult:
    """Bring the slot's window to the OS foreground and verify.

    Args:
        slot, cdp_port, cdp_ws_url: standard slot identity.
        cg_window_number, cg_window_owner_pid, cg_window_bounds:
            Pre-calibrated OS-window identity. Pass from the broker's slot
            state. If None, the function honestly reports "not calibrated"
            instead of pretending.
    """
    title = slot_title(slot)
    result = FocusResult(slot=slot, cdp_port=cdp_port, title_set=title)
    if cg_window_number is not None:
        result.cg_window_number = cg_window_number

    # Layer 1: CDP. Always run -- this also sets the page title for narration.
    try:
        await _cdp_set_title_and_activate(cdp_port, title)
        result.cdp_bring_to_front = True
    except Exception as exc:
        result.notes.append(f"CDP layer failed: {exc}")

    if sys.platform != "darwin":
        return result

    # Layer 2: AX raise of the EXACT window by geometric match.
    if cg_window_owner_pid is not None and cg_window_bounds is not None:
        from escarp.broker.macos import raise_window_by_bounds

        try:
            result.ax_raised = await asyncio.to_thread(
                raise_window_by_bounds, cg_window_owner_pid, cg_window_bounds
            )
            if not result.ax_raised:
                result.notes.append(
                    "AX raise failed (likely Accessibility permission for the "
                    "parent process not granted; grant in System Settings -> "
                    "Privacy & Security -> Accessibility)"
                )
        except Exception as exc:
            result.notes.append(f"AX raise threw: {exc}")
    else:
        result.notes.append(
            "slot not calibrated for OS-window identity; "
            "skipping AX raise. CUA bridge claim CANNOT be made for this slot."
        )

    # Layer 3: macOS app activation. Helpful, but not authoritative.
    result.os_app_activated = _osascript_activate_app(app_name, result.notes)

    # Layer 4 (ADVERSARIAL): verify the CG window is actually frontmost.
    if cg_window_number is not None:
        from escarp.broker.macos import enumerate_cft_windows, wait_for_frontmost

        result.verified_frontmost = await asyncio.to_thread(
            wait_for_frontmost, cg_window_number, timeout_s=1.5
        )
        if not result.verified_frontmost:
            # Capture who's ACTUALLY frontmost so the user gets useful debug info.
            frontmost = next(
                (w for w in enumerate_cft_windows() if w.layer == 0),
                None,
            )
            if frontmost is not None:
                result.actually_frontmost_cg_window = frontmost.window_number
                result.notes.append(
                    f"post-focus check FAILED: window {frontmost.window_number} "
                    f"(pid {frontmost.owner_pid}, bounds {frontmost.bounds}) is "
                    f"frontmost, not slot {slot}'s window "
                    f"{cg_window_number}."
                )
            else:
                result.notes.append("post-focus check FAILED: no CfT window is frontmost")
    else:
        result.notes.append(
            "no cg_window_number available; cannot verify focus. "
            "CUA bridge claim not made."
        )

    return result


async def _cdp_set_title_and_activate(cdp_port: int, title: str) -> None:
    """Sets document.title and calls Page.bringToFront on the slot's tab."""
    import json as _json

    import aiohttp

    async with httpx.AsyncClient(timeout=5.0) as client:
        tabs = (await client.get(f"http://127.0.0.1:{cdp_port}/json/list")).json()
    page_target = next(
        (t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")),
        None,
    )
    if page_target is None:
        raise RuntimeError("no page-type CDP target found")

    # JSON-encode the title -- defense-in-depth so a future caller that lets
    # an untrusted string reach `title` cannot break out of the JS string and
    # inject expressions. JSON-string-literal syntax is a strict subset of JS
    # string-literal syntax, so this is safe to drop directly into the
    # Runtime.evaluate expression.
    js_literal = _json.dumps(title)
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(page_target["webSocketDebuggerUrl"]) as ws:
            await ws.send_json(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {"expression": f"document.title = {js_literal}"},
                }
            )
            await ws.receive_json(timeout=3.0)
            await ws.send_json({"id": 2, "method": "Page.bringToFront"})
            await ws.receive_json(timeout=3.0)


def _osascript_activate_app(app_name: str, notes: list[str]) -> bool:
    if shutil.which("osascript") is None:
        notes.append("osascript not on PATH; cannot activate app")
        return False
    try:
        subprocess.run(
            ["osascript", "-e", f'tell application "{app_name}" to activate'],
            check=True,
            timeout=3.0,
            capture_output=True,
        )
        return True
    except subprocess.CalledProcessError as exc:
        notes.append(f"osascript activate failed: {exc.stderr.decode(errors='replace').strip()}")
        return False
    except subprocess.TimeoutExpired:
        notes.append("osascript activate timed out")
        return False
