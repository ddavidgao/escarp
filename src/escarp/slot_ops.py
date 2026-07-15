"""Shared low-level slot operations for the `scale` and `pool` CLIs.

These are the lifecycle-owning bits that live OUTSIDE the daemon (per the
persistence contract the broker never touches chrome processes): finding the
running broker, terminating the chrome on a slot's cdp port, and deleting a
removed slot's on-disk data.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from escarp.broker.api import DEFAULT_PORT
from escarp.broker.browser import find_cft_binary
from escarp.broker.cua_apps import (
    existing_cua_slot_app,
    slot_app_path,
    terminate_cua_slot_app_processes,
)
from escarp.broker.daemon import DAEMON_PIDFILE
from escarp.broker.procs import pid_alive, scan_escarp_chromes, terminate_pids
from escarp.broker.slots import DEFAULT_LOCK_DIR, profile_dir_for_slot
from escarp.pool_config import DEFAULT_CONFIG_PATH, PoolConfig


def broker_status(api_port: int) -> dict[str, Any] | None:
    try:
        r = httpx.get(f"http://127.0.0.1:{api_port}/status", timeout=2.0)
    except httpx.HTTPError:
        return None
    return r.json() if r.status_code == 200 else None


def find_broker_port() -> int | None:
    """Find the running broker port.

    Honor an explicit ESCARP_BROKER_URL or ESCARP_API_PORT first. The broker
    binds the preferred API port, shifting by +10 on collision, so walk that
    same ladder from the configured base.
    """
    raw_url = os.environ.get("ESCARP_BROKER_URL")
    if raw_url:
        parsed = urlparse(raw_url)
        if parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port is not None:
            for i in range(10):
                port = parsed.port + i * 10
                if broker_status(port) is not None:
                    return port
            return None

    raw_port = os.environ.get("ESCARP_API_PORT")
    if raw_port is not None:
        try:
            base = int(raw_port)
        except ValueError:
            return None
    else:
        base = DEFAULT_PORT

    for i in range(10):
        port = base + i * 10
        if broker_status(port) is not None:
            return port
    return None


def leased_slots() -> list[int]:
    """Slots the running broker reports as leased to an agent ([] when no
    broker is up to ask)."""
    port = find_broker_port()
    if port is None:
        return []
    status = broker_status(port)
    if not status:
        return []
    return sorted({s["slot"] for s in status.get("slots", []) if s.get("state") == "leased"})


def broker_url() -> str:
    """Return the best broker base URL for CLI commands."""
    raw_url = os.environ.get("ESCARP_BROKER_URL")
    if raw_url:
        parsed = urlparse(raw_url)
        if parsed.hostname not in {"127.0.0.1", "localhost"}:
            return raw_url.rstrip("/")
    port = find_broker_port()
    if port is not None:
        return f"http://127.0.0.1:{port}"
    if raw_url:
        return raw_url.rstrip("/")
    raw_port = os.environ.get("ESCARP_API_PORT")
    if raw_port is not None:
        return f"http://127.0.0.1:{raw_port}"
    return f"http://127.0.0.1:{DEFAULT_PORT}"


def pids_listening_on(port: int) -> list[int]:
    """PIDs holding a LISTEN socket on a TCP port, via lsof (macOS + Linux)."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    return [int(x) for x in out.split() if x.strip().isdigit()]


def terminate_chrome_on_port(port: int, *, grace: float = 3.0) -> bool:
    """SIGTERM (then SIGKILL) whatever chrome holds this cdp port. Returns True if
    something was there to kill."""
    pids = pids_listening_on(port)
    if not pids:
        return False
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not pids_listening_on(port):
            return True
        time.sleep(0.1)
    for pid in pids_listening_on(port):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    return True


def terminate_slot_chromes(slot: int, port: int, *, cua_apps: bool, grace: float = 3.0) -> bool:
    """Terminate all known Chrome processes for a slot.

    In standard mode the CDP port identifies the process. In CUA app mode, also
    reconcile by the per-slot macOS bundle identity so stale app windows that no
    longer own the CDP port do not survive remove/scale-down and later duplicate.
    """
    killed = terminate_chrome_on_port(port, grace=grace)
    if cua_apps:
        killed = bool(terminate_cua_slot_app_processes(slot, grace=grace)) or killed
    # Port and bundle identity both miss a plain-mode chrome that crashed off
    # its CDP port; the profile-dir process signature is the ground truth.
    strays = [p.pid for p in scan_escarp_chromes() if p.slot == slot]
    if strays:
        killed = bool(terminate_pids(strays, grace=grace)) or killed
    return killed


def read_daemon_pidfile() -> tuple[int, int] | None:
    """Return (pid, api_port) from the daemon pidfile, or None if absent/corrupt."""
    try:
        parts = DAEMON_PIDFILE.read_text().split()
        return int(parts[0]), (int(parts[1]) if len(parts) > 1 else DEFAULT_PORT)
    except (FileNotFoundError, ValueError, IndexError):
        return None


def _pid_command(pid: int) -> str:
    """The pid's current command line per ps, '' if it can't be read."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    return out.strip()


def find_daemon_pid() -> int | None:
    """The running daemon's pid: the pidfile pid if it is alive AND still an
    escarp daemon, else whoever is listening on the broker port. None if no
    daemon is running.

    The identity check matters: the pidfile survives crashes and reboots, so
    its pid may have been reused by an unrelated process. A pid whose command
    line is positively something else means the pidfile is stale -- clean it
    up, never signal it. An unreadable command line proves nothing, so the
    pidfile is kept and the port probe decides."""
    info = read_daemon_pidfile()
    if info is not None and pid_alive(info[0]):
        command = _pid_command(info[0])
        if "escarp" in command and "daemon" in command:
            return info[0]
        if command:
            with contextlib.suppress(OSError):
                DAEMON_PIDFILE.unlink(missing_ok=True)
    port = find_broker_port()
    if port is not None:
        pids = pids_listening_on(port)
        if pids:
            return pids[0]
    return None


def stop_daemon(pid: int, *, grace: float = 8.0) -> bool:
    """SIGTERM the daemon and wait for it to exit. Returns True once it is gone."""
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.15)
    return not pid_alive(pid)


def remove_slot_data(slot: int, *, cua_apps: bool) -> None:
    shutil.rmtree(profile_dir_for_slot(slot), ignore_errors=True)
    if cua_apps:
        shutil.rmtree(slot_app_path(slot), ignore_errors=True)
    with contextlib.suppress(OSError):
        (DEFAULT_LOCK_DIR / f"slot-{slot}.lock").unlink(missing_ok=True)


def resolve_cft_binary(cfg: PoolConfig) -> Path | None:
    """Prefer the binary the pool was launched with (persisted, cwd-independent);
    fall back to the cwd-relative search only if nothing was persisted."""
    if cfg.cft_binary:
        persisted = Path(cfg.cft_binary)
        if persisted.exists():
            return persisted
    return find_cft_binary()


def resolve_cua_apps(arg: bool | None, cfg: PoolConfig) -> bool:
    """Resolve CUA-app mode: explicit flag wins, else the persisted value, else
    infer from whether slot 0 already has a per-slot bundle."""
    if arg is not None:
        return arg
    if DEFAULT_CONFIG_PATH.exists():
        return cfg.cua_apps
    return existing_cua_slot_app(0) is not None
