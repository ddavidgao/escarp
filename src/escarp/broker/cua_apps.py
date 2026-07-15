"""Per-slot macOS app identities for native Codex CUA.

Codex CUA targets an app, then drives that app's key window. Two windows from
one Chrome for Testing bundle are therefore not independently addressable. For
native CUA concurrency on macOS, each slot needs its own app bundle identity.

This module creates lightweight per-slot Chrome for Testing app clones under
~/.escarp/cua-apps. On APFS, `cp -cR` clone-copies file data, so disk overhead is
mostly metadata until files diverge.
"""

from __future__ import annotations

import importlib
import plistlib
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CUA_APPS_DIR = Path.home() / ".escarp" / "cua-apps"
BASE_BUNDLE_ID = "dev.escarp.chrome"
EXECUTABLE_NAME = "Google Chrome for Testing"


class CuaAppError(RuntimeError):
    pass


@dataclass(frozen=True)
class CuaSlotApp:
    slot: int
    app_path: Path
    binary_path: Path
    bundle_id: str
    display_name: str


def slot_bundle_id(slot: int) -> str:
    if slot < 0:
        raise ValueError(f"slot must be >= 0, got {slot}")
    return f"{BASE_BUNDLE_ID}.slot{slot}"


def slot_display_name(slot: int) -> str:
    if slot < 0:
        raise ValueError(f"slot must be >= 0, got {slot}")
    return f"Escarp Chrome Slot {slot}"


def slot_app_path(slot: int, *, root: Path | None = None) -> Path:
    return (root or DEFAULT_CUA_APPS_DIR) / f"{slot_display_name(slot)}.app"


def source_app_from_binary(binary: Path) -> Path:
    """Resolve `.../*.app/Contents/MacOS/<executable>` to the .app root."""
    binary = binary.resolve()
    for parent in binary.parents:
        if parent.suffix == ".app":
            return parent
    raise CuaAppError(f"Chrome binary is not inside a .app bundle: {binary}")


def ensure_cua_slot_app(
    *,
    slot: int,
    cft_binary: Path,
    root: Path | None = None,
    force: bool = False,
) -> CuaSlotApp:
    """Create or update the per-slot app bundle and return launch metadata."""
    if sys.platform != "darwin":
        raise CuaAppError("CUA slot app bundles are only supported on macOS")

    source_app = source_app_from_binary(cft_binary)
    app_path = slot_app_path(slot, root=root)
    bundle_id = slot_bundle_id(slot)
    display_name = slot_display_name(slot)

    if force and app_path.exists():
        shutil.rmtree(app_path)
    if not app_path.exists():
        app_path.parent.mkdir(parents=True, exist_ok=True)
        _clone_app(source_app, app_path)

    _rewrite_info_plist(app_path, bundle_id=bundle_id, display_name=display_name)
    _ad_hoc_codesign(app_path)

    binary_path = app_path / "Contents" / "MacOS" / EXECUTABLE_NAME
    if not binary_path.exists():
        raise CuaAppError(f"slot app missing Chrome executable: {binary_path}")
    return CuaSlotApp(
        slot=slot,
        app_path=app_path,
        binary_path=binary_path,
        bundle_id=bundle_id,
        display_name=display_name,
    )


def existing_cua_slot_app(slot: int, *, root: Path | None = None) -> CuaSlotApp | None:
    app_path = slot_app_path(slot, root=root)
    binary_path = app_path / "Contents" / "MacOS" / EXECUTABLE_NAME
    plist_path = app_path / "Contents" / "Info.plist"
    if not app_path.exists() or not binary_path.exists() or not plist_path.exists():
        return None
    bundle_id = slot_bundle_id(slot)
    display_name = slot_display_name(slot)
    return CuaSlotApp(
        slot=slot,
        app_path=app_path,
        binary_path=binary_path,
        bundle_id=bundle_id,
        display_name=display_name,
    )


def running_slot_app_pids(slot: int) -> list[int]:
    """Return running macOS app PIDs for this slot's bundle identity."""
    if sys.platform != "darwin":
        return []
    try:
        appkit = importlib.import_module("AppKit")
    except ImportError:
        return []

    apps = appkit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(
        slot_bundle_id(slot)
    )
    return [int(app.processIdentifier()) for app in apps]


def terminate_cua_slot_app_processes(
    slot: int,
    *,
    exclude_pids: set[int] | None = None,
    grace: float = 3.0,
) -> list[int]:
    """Terminate running macOS app instances for a slot bundle identity.

    CDP-port ownership is not sufficient in CUA app mode: a stale app process can
    remain visible after losing its debugging port. If Escarp then launches a new
    process for the same slot, macOS shows duplicate "Escarp Chrome Slot N"
    windows. This helper reconciles by the slot's bundle ID before relaunching
    or deleting slot data.

    Returns the PIDs that were targeted.
    """
    if sys.platform != "darwin":
        return []
    try:
        appkit = importlib.import_module("AppKit")
    except ImportError:
        return []

    exclude_pids = exclude_pids or set()
    bundle_id = slot_bundle_id(slot)

    def targets() -> list[Any]:
        return [
            app
            for app in appkit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(
                bundle_id
            )
            if int(app.processIdentifier()) not in exclude_pids
        ]

    apps = targets()
    pids = [int(app.processIdentifier()) for app in apps]
    for app in apps:
        app.terminate()

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not targets():
            return pids
        time.sleep(0.1)

    for app in targets():
        app.forceTerminate()
    return pids


def _clone_app(source_app: Path, app_path: Path) -> None:
    try:
        subprocess.run(
            ["cp", "-cR", str(source_app), str(app_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
            timeout=120,
        )
        return
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        if app_path.exists():
            shutil.rmtree(app_path)
    shutil.copytree(source_app, app_path, symlinks=True)


def _rewrite_info_plist(app_path: Path, *, bundle_id: str, display_name: str) -> None:
    plist_path = app_path / "Contents" / "Info.plist"
    if not plist_path.exists():
        raise CuaAppError(f"app bundle missing Info.plist: {plist_path}")
    with plist_path.open("rb") as f:
        info = plistlib.load(f)

    info["CFBundleIdentifier"] = bundle_id
    info["CFBundleName"] = display_name
    info["CFBundleDisplayName"] = display_name
    info["CFBundleExecutable"] = EXECUTABLE_NAME

    with plist_path.open("wb") as f:
        plistlib.dump(info, f)


def _ad_hoc_codesign(app_path: Path) -> None:
    if shutil.which("codesign") is None:
        return
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(app_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=120,
    )
