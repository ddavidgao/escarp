"""Process-table scan for escarp-owned chromes."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from escarp.broker import procs as pr


def _fake_ps(monkeypatch, stdout: str) -> None:
    monkeypatch.setattr(
        pr.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout=stdout),
    )


def test_scan_matches_signature_and_parses_slot(monkeypatch) -> None:
    root = Path("/Users/x/.escarp/profiles")
    stdout = (
        f"  101 /Applications/Chrome --user-data-dir={root}/autonomous/slot-0 --remote-debugging-port=9222\n"
        f"  102 /Applications/Chrome --user-data-dir={root}/autonomous/slot-17 about:blank\n"
        f"  103 /Applications/Chrome --user-data-dir={root}/autonomous/slot-1 --type=renderer\n"
        f"  104 /Applications/Chrome --user-data-dir=/Users/x/other/slot-2\n"
        f"  105 /Applications/Chrome --user-data-dir={root}/weird\n"
        f"garbage line\n"
    )
    _fake_ps(monkeypatch, stdout)

    found = pr.scan_escarp_chromes(profile_root=root)
    assert [(p.pid, p.slot) for p in found] == [(101, 0), (102, 17), (105, None)]


def test_scan_survives_ps_failure(monkeypatch) -> None:
    def boom(*a, **k):
        raise subprocess.SubprocessError("ps died")

    monkeypatch.setattr(pr.subprocess, "run", boom)
    assert pr.scan_escarp_chromes() == []


def test_terminate_pids_kills_real_process() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        signaled = pr.terminate_pids([proc.pid], grace=5.0)
        assert signaled == [proc.pid]
        proc.wait(timeout=5)
        assert not pr.pid_alive(proc.pid)
    finally:
        if proc.poll() is None:
            os.kill(proc.pid, signal.SIGKILL)


def test_terminate_pids_skips_already_dead() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    time.sleep(0.05)
    assert pr.terminate_pids([proc.pid], grace=0.5) == []
