from __future__ import annotations

import plistlib
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from escarp.broker.cua_apps import (
    EXECUTABLE_NAME,
    CuaAppError,
    ensure_cua_slot_app,
    existing_cua_slot_app,
    running_slot_app_pids,
    slot_bundle_id,
    slot_display_name,
    source_app_from_binary,
    terminate_cua_slot_app_processes,
)


def _source_app(tmp_path: Path) -> Path:
    app = tmp_path / "Google Chrome for Testing.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    binary = macos / EXECUTABLE_NAME
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    plist = app / "Contents" / "Info.plist"
    with plist.open("wb") as f:
        plistlib.dump(
            {
                "CFBundleIdentifier": "com.google.chrome.for.testing",
                "CFBundleName": "Google Chrome for Testing",
                "CFBundleExecutable": EXECUTABLE_NAME,
            },
            f,
        )
    return app


def test_slot_identity_names_are_stable() -> None:
    assert slot_bundle_id(0) == "dev.escarp.chrome.slot0"
    assert slot_bundle_id(12) == "dev.escarp.chrome.slot12"
    assert slot_display_name(1) == "Escarp Chrome Slot 1"


def test_source_app_from_binary() -> None:
    binary = Path(
        "/tmp/Chrome.app/Contents/MacOS/Google Chrome for Testing"
    )
    assert source_app_from_binary(binary) == Path("/tmp/Chrome.app").resolve()


def test_source_app_rejects_non_app_binary(tmp_path: Path) -> None:
    binary = tmp_path / "chrome"
    binary.write_text("")
    with pytest.raises(CuaAppError):
        source_app_from_binary(binary)


def test_ensure_cua_slot_app_rewrites_info_plist(tmp_path: Path) -> None:
    source_app = _source_app(tmp_path / "source")
    cft_binary = source_app / "Contents" / "MacOS" / EXECUTABLE_NAME

    with patch("sys.platform", "darwin"), \
         patch("escarp.broker.cua_apps._ad_hoc_codesign"):
        slot_app = ensure_cua_slot_app(
            slot=2,
            cft_binary=cft_binary,
            root=tmp_path / "apps",
        )

    assert slot_app.bundle_id == "dev.escarp.chrome.slot2"
    assert slot_app.display_name == "Escarp Chrome Slot 2"
    assert slot_app.binary_path.exists()

    with (slot_app.app_path / "Contents" / "Info.plist").open("rb") as f:
        info = plistlib.load(f)
    assert info["CFBundleIdentifier"] == "dev.escarp.chrome.slot2"
    assert info["CFBundleName"] == "Escarp Chrome Slot 2"
    assert info["CFBundleDisplayName"] == "Escarp Chrome Slot 2"
    assert info["CFBundleExecutable"] == EXECUTABLE_NAME


def test_existing_cua_slot_app_detects_materialized_app(tmp_path: Path) -> None:
    source_app = _source_app(tmp_path / "source")
    cft_binary = source_app / "Contents" / "MacOS" / EXECUTABLE_NAME

    with patch("sys.platform", "darwin"), \
         patch("escarp.broker.cua_apps._ad_hoc_codesign"):
        created = ensure_cua_slot_app(
            slot=3,
            cft_binary=cft_binary,
            root=tmp_path / "apps",
        )

    found = existing_cua_slot_app(3, root=tmp_path / "apps")
    assert found == created


def test_running_slot_app_pids_uses_slot_bundle_id(monkeypatch) -> None:
    seen: list[str] = []

    class FakeApp:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def processIdentifier(self) -> int:
            return self.pid

    class FakeNSRunningApplication:
        @staticmethod
        def runningApplicationsWithBundleIdentifier_(bundle_id: str):
            seen.append(bundle_id)
            return [FakeApp(101), FakeApp(202)]

    fake_appkit = types.SimpleNamespace(NSRunningApplication=FakeNSRunningApplication)
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)

    assert running_slot_app_pids(7) == [101, 202]
    assert seen == ["dev.escarp.chrome.slot7"]


def test_terminate_cua_slot_app_processes_excludes_current_pid(monkeypatch) -> None:
    terminated: list[int] = []
    forced: list[int] = []
    calls = 0

    class FakeApp:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def processIdentifier(self) -> int:
            return self.pid

        def terminate(self) -> None:
            terminated.append(self.pid)

        def forceTerminate(self) -> None:
            forced.append(self.pid)

    class FakeNSRunningApplication:
        @staticmethod
        def runningApplicationsWithBundleIdentifier_(bundle_id: str):
            nonlocal calls
            calls += 1
            # First call sees stale + current. Polling calls see only the
            # excluded/current app, so graceful termination is enough.
            if calls == 1:
                return [FakeApp(111), FakeApp(222)]
            return [FakeApp(222)]

    fake_appkit = types.SimpleNamespace(NSRunningApplication=FakeNSRunningApplication)
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)

    assert terminate_cua_slot_app_processes(1, exclude_pids={222}) == [111]
    assert terminated == [111]
    assert forced == []
