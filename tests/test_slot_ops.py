"""Shared slot-operation helpers."""

from __future__ import annotations

from escarp import slot_ops
from escarp.broker.procs import ChromeProc


def test_terminate_slot_chromes_kills_port_dead_stray(monkeypatch) -> None:
    # A crashed chrome holds no LISTEN socket, so the port kill finds nothing;
    # the profile-signature scan must still reap it.
    killed: list[list[int]] = []
    monkeypatch.setattr(slot_ops, "pids_listening_on", lambda port: [])
    monkeypatch.setattr(
        slot_ops,
        "scan_escarp_chromes",
        lambda **k: [ChromeProc(pid=333, slot=2, command="c"), ChromeProc(pid=444, slot=3, command="c")],
    )
    monkeypatch.setattr(slot_ops, "terminate_pids", lambda pids, **k: killed.append(list(pids)) or list(pids))

    assert slot_ops.terminate_slot_chromes(2, 9224, cua_apps=False) is True
    assert killed == [[333]]  # only slot 2's stray, not slot 3's


def test_terminate_slot_chromes_reports_nothing_to_kill(monkeypatch) -> None:
    monkeypatch.setattr(slot_ops, "pids_listening_on", lambda port: [])
    monkeypatch.setattr(slot_ops, "scan_escarp_chromes", lambda **k: [])

    assert slot_ops.terminate_slot_chromes(2, 9224, cua_apps=False) is False


def test_find_broker_port_honors_broker_url(monkeypatch) -> None:
    checked: list[int] = []

    def fake_status(port: int):
        checked.append(port)
        return {"slots": []} if port == 17878 else None

    monkeypatch.setenv("ESCARP_BROKER_URL", "http://127.0.0.1:17878")
    monkeypatch.setattr(slot_ops, "broker_status", fake_status)

    assert slot_ops.find_broker_port() == 17878
    assert checked == [17878]


def test_find_broker_port_honors_api_port_shift(monkeypatch) -> None:
    checked: list[int] = []

    def fake_status(port: int):
        checked.append(port)
        return {"slots": []} if port == 17888 else None

    monkeypatch.setenv("ESCARP_API_PORT", "17878")
    monkeypatch.setattr(slot_ops, "broker_status", fake_status)

    assert slot_ops.find_broker_port() == 17888
    assert checked == [17878, 17888]


def test_broker_url_uses_configured_local_broker(monkeypatch) -> None:
    monkeypatch.setenv("ESCARP_BROKER_URL", "http://127.0.0.1:17878/")
    monkeypatch.setattr(slot_ops, "broker_status", lambda port: {"slots": []})

    assert slot_ops.broker_url() == "http://127.0.0.1:17878"


def test_broker_url_preserves_nonlocal_broker(monkeypatch) -> None:
    monkeypatch.setenv("ESCARP_BROKER_URL", "https://broker.example.test/escarp/")

    assert slot_ops.broker_url() == "https://broker.example.test/escarp"


def test_find_daemon_pid_rejects_reused_pidfile_pid(monkeypatch, tmp_path) -> None:
    # The pidfile survives crashes and reboots; if its pid now belongs to an
    # unrelated process, never signal it -- clean the stale pidfile instead.
    pidfile = tmp_path / "daemon.pid"
    pidfile.write_text("12345 7878\n")
    monkeypatch.setattr(slot_ops, "DAEMON_PIDFILE", pidfile)
    monkeypatch.setattr(slot_ops, "pid_alive", lambda pid: True)
    monkeypatch.setattr(slot_ops, "_pid_command", lambda pid: "/usr/bin/unrelated-tool --serve")
    monkeypatch.setattr(slot_ops, "find_broker_port", lambda: None)

    assert slot_ops.find_daemon_pid() is None
    assert not pidfile.exists()


def test_find_daemon_pid_trusts_verified_daemon(monkeypatch, tmp_path) -> None:
    pidfile = tmp_path / "daemon.pid"
    pidfile.write_text("12345 7878\n")
    monkeypatch.setattr(slot_ops, "DAEMON_PIDFILE", pidfile)
    monkeypatch.setattr(slot_ops, "pid_alive", lambda pid: True)
    monkeypatch.setattr(
        slot_ops, "_pid_command", lambda pid: "/x/.venv/bin/python /x/.venv/bin/escarp daemon"
    )

    assert slot_ops.find_daemon_pid() == 12345
    assert pidfile.exists()


def test_find_daemon_pid_keeps_pidfile_when_ps_unreadable(monkeypatch, tmp_path) -> None:
    # ps failing proves nothing about identity: fall through to the port probe
    # without signaling and without deleting the pidfile.
    pidfile = tmp_path / "daemon.pid"
    pidfile.write_text("12345 7878\n")
    monkeypatch.setattr(slot_ops, "DAEMON_PIDFILE", pidfile)
    monkeypatch.setattr(slot_ops, "pid_alive", lambda pid: True)
    monkeypatch.setattr(slot_ops, "_pid_command", lambda pid: "")
    monkeypatch.setattr(slot_ops, "find_broker_port", lambda: None)

    assert slot_ops.find_daemon_pid() is None
    assert pidfile.exists()
