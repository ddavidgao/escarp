"""Bring a slot's CfT window to the OS foreground.

v1.1's main CUA integration primitive. Per the static-analysis verdict (see
research/cua_targeting.md), Codex CUA targets the **key window of an app**.
For escarp's pool to work with CUA at all, the broker has to be able to say
"make slot N's window the key window" — without that, CUA acts on whichever
CfT window happens to be foreground.

Three-layer approach (belt + suspenders + lifeline):

  1. CDP `Page.bringToFront` -- activates the tab and, in practice on macOS,
     promotes the containing window to key within Chrome.
  2. macOS `osascript activate` -- brings the Chrome for Testing app to the
     foreground over other apps (does nothing about which window of CfT is
     key).
  3. macOS `System Events` window-index manipulation by title -- if multiple
     CfT windows exist, this is the only way to pick a specific one. We label
     each slot's window via `document.title = "escarp-slot-N"` so it's
     addressable.

Layer 1 alone is usually sufficient; layers 2 + 3 are belt + suspenders for
multi-window pools. On non-macOS, layer 3 is a no-op and layer 2 is best-
effort (e.g. wmctrl if available).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from dataclasses import dataclass

import httpx


# Constant title we set on each slot's tab. Codex CUA's narration may reference
# the title; more importantly, AppleScript can address windows by name suffix.
SLOT_TITLE_PREFIX = "escarp-slot-"


def slot_title(slot: int) -> str:
    return f"{SLOT_TITLE_PREFIX}{slot}"


@dataclass
class FocusResult:
    slot: int
    cdp_port: int
    title_set: str
    cdp_bring_to_front: bool
    os_app_activated: bool
    os_window_promoted: bool
    notes: list[str]

    def succeeded(self) -> bool:
        # Layer 1 is the load-bearing one for single-window pools; on macOS we
        # also want at least the app activation. Per-window promotion is only
        # required if the pool has >1 visible CfT window — but we always try.
        return self.cdp_bring_to_front and (sys.platform != "darwin" or self.os_app_activated)


async def focus_slot(
    *,
    slot: int,
    cdp_port: int,
    cdp_ws_url: str,
    app_name: str = "Google Chrome for Testing",
) -> FocusResult:
    """Bring the slot's window to the OS foreground. Idempotent.

    Sets the page title to `escarp-slot-<N>` along the way so the window can
    be addressed by title in AppleScript / by humans on screen.
    """
    notes: list[str] = []
    title = slot_title(slot)

    # Layer 1: CDP. Set title + bring tab to front in one round trip.
    cdp_ok = False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            tabs = (await client.get(f"http://127.0.0.1:{cdp_port}/json/list")).json()
        page_target = next(
            (t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")),
            None,
        )
        if page_target is None:
            notes.append("no page-type CDP target found; nothing to bring forward")
        else:
            await _cdp_set_title_and_activate(page_target["webSocketDebuggerUrl"], title)
            cdp_ok = True
    except Exception as exc:
        notes.append(f"CDP layer failed: {exc}")

    # Layer 2: macOS app activation.
    os_app_ok = False
    if sys.platform == "darwin":
        os_app_ok = _osascript_activate_app(app_name, notes)

    # Layer 3: per-window promotion by title.
    os_window_ok = False
    if sys.platform == "darwin":
        os_window_ok = _osascript_promote_window(app_name, title, notes)

    return FocusResult(
        slot=slot,
        cdp_port=cdp_port,
        title_set=title,
        cdp_bring_to_front=cdp_ok,
        os_app_activated=os_app_ok,
        os_window_promoted=os_window_ok,
        notes=notes,
    )


async def _cdp_set_title_and_activate(page_ws_url: str, title: str) -> None:
    """Connect to a tab's CDP websocket and run Runtime.evaluate + Page.bringToFront."""
    import aiohttp

    safe_title = title.replace("'", "\\'")
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(page_ws_url) as ws:
            await ws.send_json(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {"expression": f"document.title = '{safe_title}'"},
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


def _osascript_promote_window(app_name: str, title_substr: str, notes: list[str]) -> bool:
    """Promote the window whose title contains `title_substr` to key within `app_name`.

    Requires Accessibility permission for whatever app launched this process.
    Failure is non-fatal — `Page.bringToFront` already does the heavy lifting
    on single-window pools, and CUA's "key window" tracking re-queries each
    turn. We log and move on.
    """
    if shutil.which("osascript") is None:
        return False
    script = (
        'tell application "System Events"\n'
        f'  tell process "{app_name}"\n'
        f'    set targetWindows to (every window whose name contains "{title_substr}")\n'
        '    if (count of targetWindows) > 0 then\n'
        '      set frontmost to true\n'
        '      perform action "AXRaise" of (item 1 of targetWindows)\n'
        '      return "ok"\n'
        '    end if\n'
        '    return "no-match"\n'
        '  end tell\n'
        'end tell'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=True,
            timeout=3.0,
            capture_output=True,
        )
        out = result.stdout.decode().strip()
        if out == "no-match":
            notes.append(f"no CfT window with title containing '{title_substr}'")
            return False
        return True
    except subprocess.CalledProcessError as exc:
        msg = exc.stderr.decode(errors="replace").strip()
        # Accessibility permission errors are common on first run.
        if "-25211" in msg or "not allowed assistive access" in msg.lower():
            notes.append(
                "AppleScript needs Accessibility permission for the parent process "
                "(System Preferences -> Privacy & Security -> Accessibility). "
                "Skipping per-window promotion; CDP bring_to_front already fired."
            )
        else:
            notes.append(f"osascript per-window promotion failed: {msg}")
        return False
    except subprocess.TimeoutExpired:
        notes.append("osascript per-window promotion timed out")
        return False
