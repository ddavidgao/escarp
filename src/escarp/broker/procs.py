"""Process-level reality for escarp-owned chromes.

CDP-port probing only sees healthy chromes: a crashed or hung chrome that lost
its debugging port is invisible to the port scan, which is exactly how stale
processes accumulate (the port scan says "slot missing", a fresh chrome gets
launched, and the corpse lives on). Every escarp-launched chrome carries an
unambiguous command-line signature (--user-data-dir under the escarp profile
root), so this module scans the process table for that signature as the ground
truth, independent of CDP responsiveness. CUA-app-mode chromes match too: the
per-slot bundles run with the same profile flag.

Only the main browser process is matched (helper processes carry --type=...);
killing the main process tears its helpers down with it.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from escarp.broker.slots import DEFAULT_PROFILE_DIR

_SLOT_RE = re.compile(r"--user-data-dir=\S*/slot-(\d+)")


@dataclass(frozen=True)
class ChromeProc:
    pid: int
    slot: int | None  # None if the profile path didn't parse to a slot index
    command: str


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def scan_escarp_chromes(*, profile_root: Path | None = None) -> list[ChromeProc]:
    """Return the main chrome processes launched from the escarp profile root."""
    root = str(profile_root or DEFAULT_PROFILE_DIR)
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        return []

    procs: list[ChromeProc] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, command = int(parts[0]), parts[1]
        if f"--user-data-dir={root}/" not in command:
            continue
        if "--type=" in command:
            continue
        match = _SLOT_RE.search(command)
        procs.append(
            ChromeProc(
                pid=pid,
                slot=int(match.group(1)) if match else None,
                command=command,
            )
        )
    return procs


def terminate_pids(pids: list[int], *, grace: float = 3.0, reverify: bool = False) -> list[int]:
    """SIGTERM (then SIGKILL) each pid. Returns the pids that were alive to signal.

    reverify=True re-scans the process table and drops pids that no longer
    carry the escarp chrome signature: kill lists are captured seconds before
    the signal, and a pid the OS reused in that window must never be hit."""
    if reverify:
        current = {p.pid for p in scan_escarp_chromes()}
        pids = [pid for pid in pids if pid in current]
    targets = [pid for pid in pids if pid_alive(pid)]
    for pid in targets:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not any(pid_alive(pid) for pid in targets):
            return targets
        time.sleep(0.1)
    for pid in targets:
        if pid_alive(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
    return targets
