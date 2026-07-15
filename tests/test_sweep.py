"""Daemon process sweep: orphan and corpse reaping with strike counters."""

from __future__ import annotations

import escarp.broker.sweep as sw
from escarp.broker.lease import Broker
from escarp.broker.pool import PoolController
from escarp.broker.procs import ChromeProc
from escarp.broker.sweep import SweepState, sweep_once


def _proc(pid: int, slot: int | None) -> ChromeProc:
    return ChromeProc(pid=pid, slot=slot, command=f"chrome slot-{slot}")


def _pool(slots: list[int]) -> tuple[Broker, PoolController]:
    broker = Broker()
    for slot in slots:
        broker.register(slot=slot, cdp_port=9222 + slot, cdp_ws_url=f"ws://x/{slot}", pid=-1)
    return broker, PoolController(broker)


def _patch(monkeypatch, *, procs, alive_ports):
    killed: list[list[int]] = []

    async def fake_probe(port, *, timeout=2.0):
        return {"Browser": "x"} if port in alive_ports else None

    monkeypatch.setattr(sw, "scan_escarp_chromes", lambda: list(procs))
    monkeypatch.setattr(sw, "probe", fake_probe)
    monkeypatch.setattr(
        sw, "terminate_pids", lambda pids, **k: killed.append(list(pids)) or list(pids)
    )
    return killed


async def test_orphan_reaped_after_consecutive_strikes(monkeypatch) -> None:
    broker, controller = _pool([0])
    killed = _patch(monkeypatch, procs=[_proc(500, 7)], alive_ports={9222})
    state = SweepState()

    await sweep_once(broker, controller, state)
    assert killed == []  # first sighting: strike only

    await sweep_once(broker, controller, state)
    assert killed == [[500]]  # second consecutive sighting: reaped


async def test_orphan_strike_resets_when_proc_disappears(monkeypatch) -> None:
    broker, controller = _pool([0])
    killed = _patch(monkeypatch, procs=[_proc(500, 7)], alive_ports={9222})
    state = SweepState()

    await sweep_once(broker, controller, state)
    assert state.orphan_strikes == {500: 1}

    _patch(monkeypatch, procs=[], alive_ports={9222})
    await sweep_once(broker, controller, state)
    assert state.orphan_strikes == {}
    assert killed == []


async def test_dead_slot_killed_and_unbrokered_after_strikes(monkeypatch) -> None:
    broker, controller = _pool([0, 1])
    killed = _patch(monkeypatch, procs=[_proc(600, 1)], alive_ports={9222})  # slot 1 cdp dead
    state = SweepState()

    for _ in range(sw.DEAD_STRIKES - 1):
        await sweep_once(broker, controller, state)
        assert killed == []
        assert broker.has_slot(1)

    await sweep_once(broker, controller, state)
    assert killed == [[600]]
    assert not broker.has_slot(1)
    assert broker.has_slot(0)  # healthy slot untouched


async def test_leased_slot_never_swept(monkeypatch) -> None:
    broker, controller = _pool([0])
    killed = _patch(monkeypatch, procs=[_proc(700, 0)], alive_ports=set())  # cdp dead
    await broker.acquire(holder="agent", slot=0)
    state = SweepState()

    for _ in range(sw.DEAD_STRIKES + 1):
        await sweep_once(broker, controller, state)

    assert killed == []
    assert broker.has_slot(0)


async def test_healthy_slot_clears_strikes(monkeypatch) -> None:
    broker, controller = _pool([0])
    _patch(monkeypatch, procs=[_proc(800, 0)], alive_ports=set())
    state = SweepState()
    await sweep_once(broker, controller, state)
    assert state.dead_strikes == {0: 1}

    killed = _patch(monkeypatch, procs=[_proc(800, 0)], alive_ports={9222})
    await sweep_once(broker, controller, state)
    assert state.dead_strikes == {}
    assert killed == []
