"""`escarp daemon stop` teardown behavior."""

from __future__ import annotations

import escarp.cli_daemon as cd
from escarp.broker.procs import ChromeProc


def test_stop_running_daemon(monkeypatch) -> None:
    stopped: list[int] = []
    monkeypatch.setattr(cd, "find_daemon_pid", lambda: 4242)
    monkeypatch.setattr(cd, "stop_daemon", lambda pid, **k: stopped.append(pid) or True)

    assert cd.stop_main([]) == 0
    assert stopped == [4242]


def test_stop_reports_failure(monkeypatch) -> None:
    monkeypatch.setattr(cd, "find_daemon_pid", lambda: 4242)
    monkeypatch.setattr(cd, "stop_daemon", lambda pid, **k: False)

    assert cd.stop_main([]) == 1


def test_stop_without_daemon_clears_stale_pidfile(monkeypatch, tmp_path) -> None:
    pidfile = tmp_path / "daemon.pid"
    pidfile.write_text("99999 7878\n")
    monkeypatch.setattr(cd, "DAEMON_PIDFILE", pidfile)
    monkeypatch.setattr(cd, "find_daemon_pid", lambda: None)
    monkeypatch.setattr(cd, "read_daemon_pidfile", lambda: (99999, 7878))

    assert cd.stop_main([]) == 0
    assert not pidfile.exists()


def test_kill_pool_terminates_all_escarp_chromes(monkeypatch) -> None:
    killed: list[list[int]] = []
    monkeypatch.setattr(cd, "find_daemon_pid", lambda: 4242)
    monkeypatch.setattr(cd, "stop_daemon", lambda pid, **k: True)
    monkeypatch.setattr(
        cd,
        "scan_escarp_chromes",
        lambda: [ChromeProc(pid=11, slot=0, command="c"), ChromeProc(pid=22, slot=5, command="c")],
    )
    monkeypatch.setattr(
        cd, "terminate_pids", lambda pids, **k: killed.append(list(pids)) or list(pids)
    )

    assert cd.stop_main(["--kill-pool"]) == 0
    assert killed == [[11, 22]]
