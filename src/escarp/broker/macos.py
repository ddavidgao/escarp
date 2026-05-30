"""macOS window-identity primitives via CGWindowList + AX.

Per David's note: title-based AX targeting is not authoritative. This module
provides slot -> kCGWindowNumber identity mapping and AX raise that targets
a specific OS window, with adversarial verification after the raise.

All entry points are no-ops on non-darwin and return honest "not supported"
results so the broker still works on Linux/Windows for the CDP-only path.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

CFT_APP_NAMES = ("Chrome for Testing", "Google Chrome for Testing")


@dataclass(frozen=True)
class CGWindowInfo:
    window_number: int
    owner_pid: int
    owner_name: str
    name: str | None
    bounds: tuple[float, float, float, float]  # x, y, width, height
    layer: int


def _is_darwin() -> bool:
    return sys.platform == "darwin"


def enumerate_cft_windows() -> list[CGWindowInfo]:
    """Return all on-screen, normal-layer windows owned by Chrome for Testing.

    Layer 0 = normal app windows. Skipping non-zero layers excludes
    chrome's transient overlays, devtools menus, etc.
    """
    if not _is_darwin():
        return []
    from Quartz import (  # type: ignore[import-not-found]
        CGWindowListCopyWindowInfo,
        kCGNullWindowID,
        kCGWindowListOptionOnScreenOnly,
    )

    raw = CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly, kCGNullWindowID
    )
    out: list[CGWindowInfo] = []
    for w in raw or []:
        owner_name = w.get("kCGWindowOwnerName") or ""
        if not any(name in owner_name for name in CFT_APP_NAMES):
            continue
        bounds = w.get("kCGWindowBounds") or {}
        out.append(
            CGWindowInfo(
                window_number=int(w.get("kCGWindowNumber", 0)),
                owner_pid=int(w.get("kCGWindowOwnerPID", 0)),
                owner_name=owner_name,
                name=w.get("kCGWindowName"),
                bounds=(
                    float(bounds.get("X", 0)),
                    float(bounds.get("Y", 0)),
                    float(bounds.get("Width", 0)),
                    float(bounds.get("Height", 0)),
                ),
                layer=int(w.get("kCGWindowLayer", 0)),
            )
        )
    return out


def find_by_bounds(
    target: tuple[float, float, float, float],
    *,
    tolerance: float = 10.0,
) -> CGWindowInfo | None:
    """Find the CfT window whose bounds match `target` within `tolerance` px on
    each side. Used by calibration to bind a CDP-set rectangle to its real CG
    window."""
    tx, ty, tw, th = target
    for w in enumerate_cft_windows():
        wx, wy, ww, wh = w.bounds
        if (
            abs(wx - tx) <= tolerance
            and abs(wy - ty) <= tolerance
            and abs(ww - tw) <= tolerance
            and abs(wh - th) <= tolerance
        ):
            return w
    return None


def is_frontmost(window_number: int) -> bool:
    """True iff `window_number` is the topmost on-screen CfT window (layer 0,
    first in CGWindowList enumeration order).

    CGWindowListCopyWindowInfo returns windows in z-order from front to back
    for kCGWindowListOptionOnScreenOnly. The first match for a CfT window is
    the one currently key/frontmost across the CfT app.
    """
    for w in enumerate_cft_windows():
        if w.layer == 0:
            return w.window_number == window_number
    return False


def raise_window_by_bounds(
    pid: int,
    target_bounds: tuple[float, float, float, float],
    *,
    tolerance: float = 10.0,
) -> bool:
    """Use AX to perform AXRaise on the window of `pid` whose AX bounds match
    `target_bounds`. AX doesn't expose kCGWindowNumber, so geometric match is
    the bridge.

    Returns True on success. Returns False if AX permission is missing, the
    process has no AX-visible windows, or no window matched.
    """
    if not _is_darwin():
        return False
    try:
        from ApplicationServices import (  # type: ignore[import-not-found]
            AXUIElementCopyAttributeValue,
            AXUIElementCreateApplication,
            AXUIElementPerformAction,
            kAXErrorSuccess,
            kAXPositionAttribute,
            kAXRaiseAction,
            kAXSizeAttribute,
            kAXWindowsAttribute,
        )
        from Quartz import (  # type: ignore[import-not-found]
            CGPointZero,
            CGSizeZero,
        )
    except ImportError:
        return False

    app = AXUIElementCreateApplication(pid)
    err, windows = AXUIElementCopyAttributeValue(app, kAXWindowsAttribute, None)
    if err != kAXErrorSuccess or not windows:
        return False

    tx, ty, tw, th = target_bounds
    for w in windows:
        try:
            err, pos_val = AXUIElementCopyAttributeValue(w, kAXPositionAttribute, None)
            if err != kAXErrorSuccess:
                continue
            err, size_val = AXUIElementCopyAttributeValue(w, kAXSizeAttribute, None)
            if err != kAXErrorSuccess:
                continue
            # AXValueRef -> CGPoint/CGSize. PyObjC's AXValueGetValue isn't
            # always cleanly exposed; we use the bridge that returns NSValues
            # whose CGPoint/CGSize accessors work directly.
            px = float(pos_val.pointValue().x) if hasattr(pos_val, "pointValue") else float(pos_val.x)
            py = float(pos_val.pointValue().y) if hasattr(pos_val, "pointValue") else float(pos_val.y)
            sw = float(size_val.sizeValue().width) if hasattr(size_val, "sizeValue") else float(size_val.width)
            sh = float(size_val.sizeValue().height) if hasattr(size_val, "sizeValue") else float(size_val.height)
        except Exception:
            continue
        if (
            abs(px - tx) <= tolerance
            and abs(py - ty) <= tolerance
            and abs(sw - tw) <= tolerance
            and abs(sh - th) <= tolerance
        ):
            err = AXUIElementPerformAction(w, kAXRaiseAction)
            return err == kAXErrorSuccess

    return False


def lookup_window(os_window_id: int) -> CGWindowInfo | None:
    """Find a specific window by its OS window id. Returns None if the id no
    longer corresponds to an on-screen window (process died, window closed)."""
    if not _is_darwin():
        return None
    for w in enumerate_cft_windows():
        if w.window_number == os_window_id:
            return w
    return None


@dataclass(frozen=True)
class WindowVerification:
    """Active, just-now-queried verification of a slot's OS window identity.
    Every field reflects the OS at the moment of the call, not stored state."""
    os_window_id: int
    verified_alive: bool
    verified_app: bool
    verified_bounds: bool
    verified_key: bool
    current_owner_name: str | None = None
    current_owner_pid: int | None = None
    current_bounds: tuple[float, float, float, float] | None = None
    notes: list[str] | None = None

    def all_ok(self) -> bool:
        return (
            self.verified_alive
            and self.verified_app
            and self.verified_bounds
            and self.verified_key
        )


def verify_window(
    os_window_id: int,
    *,
    expected_bounds: tuple[float, float, float, float] | None = None,
    expected_owner_pid: int | None = None,
    bounds_tolerance: float = 10.0,
) -> WindowVerification:
    """Active verification. Queries CGWindowList right now and reports:

        alive   - window still exists on screen
        app     - owner name still matches Chrome for Testing
        bounds  - current bounds match expected_bounds within tolerance
        key     - window is the topmost (layer 0, first in z-order) CfT window
    """
    if not _is_darwin():
        return WindowVerification(
            os_window_id=os_window_id,
            verified_alive=False,
            verified_app=False,
            verified_bounds=False,
            verified_key=False,
            notes=["non-darwin: OS window verification not available"],
        )

    w = lookup_window(os_window_id)
    if w is None:
        return WindowVerification(
            os_window_id=os_window_id,
            verified_alive=False,
            verified_app=False,
            verified_bounds=False,
            verified_key=False,
            notes=[f"no on-screen CfT window with id {os_window_id}"],
        )

    notes: list[str] = []
    verified_app = any(name in w.owner_name for name in CFT_APP_NAMES)
    if not verified_app:
        notes.append(f"owner name {w.owner_name!r} does not match Chrome for Testing")
    if expected_owner_pid is not None and w.owner_pid != expected_owner_pid:
        notes.append(
            f"owner pid drift: expected {expected_owner_pid}, actual {w.owner_pid}"
        )

    verified_bounds = True
    if expected_bounds is not None:
        ex, ey, ew, eh = expected_bounds
        ax, ay, aw, ah = w.bounds
        if (
            abs(ax - ex) > bounds_tolerance
            or abs(ay - ey) > bounds_tolerance
            or abs(aw - ew) > bounds_tolerance
            or abs(ah - eh) > bounds_tolerance
        ):
            verified_bounds = False
            notes.append(
                f"bounds drift: expected {expected_bounds}, actual {w.bounds}"
            )

    verified_key = is_frontmost(os_window_id)
    if not verified_key:
        # find who IS frontmost for the error message
        for other in enumerate_cft_windows():
            if other.layer == 0 and other.window_number != os_window_id:
                notes.append(
                    f"not key/frontmost; window {other.window_number} "
                    f"(pid {other.owner_pid}, bounds {other.bounds}) is."
                )
                break

    return WindowVerification(
        os_window_id=os_window_id,
        verified_alive=True,
        verified_app=verified_app,
        verified_bounds=verified_bounds,
        verified_key=verified_key,
        current_owner_name=w.owner_name,
        current_owner_pid=w.owner_pid,
        current_bounds=w.bounds,
        notes=notes if notes else None,
    )


def wait_for_frontmost(window_number: int, *, timeout_s: float = 1.5) -> bool:
    """Poll is_frontmost() up to `timeout_s` seconds. Used as the post-focus
    adversarial check.
    """
    if not _is_darwin():
        return False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if is_frontmost(window_number):
            return True
        time.sleep(0.05)
    return False
