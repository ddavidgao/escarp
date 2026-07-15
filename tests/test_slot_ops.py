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
