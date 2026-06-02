"""Broker.unregister and PoolController hot add/remove logic."""

from __future__ import annotations

import pytest

from escarp.broker import pool as pool_mod
from escarp.broker.lease import Broker, SlotLeased, UnknownSlot
from escarp.broker.pool import (
    NoBrowserOnPort,
    PoolController,
    SlotAlreadyInPool,
    SlotRegistration,
)


def _seed(broker: Broker, n: int = 2) -> None:
    for slot in range(n):
        broker.register(
            slot=slot,
            cdp_port=9222 + slot,
            cdp_ws_url=f"ws://127.0.0.1:{9222 + slot}/devtools/browser/seed-{slot}",
            pid=10000 + slot,
        )


class _FakeLease:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


# --------------------------------------------------------------------------- #
# Broker.unregister                                                           #
# --------------------------------------------------------------------------- #
async def test_unregister_free_slot_drops_record() -> None:
    broker = Broker()
    _seed(broker, 2)
    rec = await broker.unregister(slot=1)
    assert rec.slot == 1
    assert not broker.has_slot(1)
    assert broker.pool_size() == 1


async def test_unregister_unknown_slot_raises() -> None:
    broker = Broker()
    _seed(broker, 1)
    with pytest.raises(UnknownSlot):
        await broker.unregister(slot=9)


async def test_unregister_leased_without_force_refuses() -> None:
    broker = Broker()
    _seed(broker, 2)
    await broker.acquire(holder="A", slot=0)
    with pytest.raises(SlotLeased):
        await broker.unregister(slot=0)
    assert broker.has_slot(0)


async def test_unregister_leased_with_force_removes() -> None:
    broker = Broker()
    _seed(broker, 2)
    await broker.acquire(holder="A", slot=0)
    rec = await broker.unregister(slot=0, force=True)
    assert rec.slot == 0
    assert not broker.has_slot(0)


# --------------------------------------------------------------------------- #
# PoolController.add_slot                                                     #
# --------------------------------------------------------------------------- #
async def test_add_slot_already_in_pool_raises() -> None:
    broker = Broker()
    _seed(broker, 1)
    ctrl = PoolController(broker, cdp_base=9222)
    with pytest.raises(SlotAlreadyInPool):
        await ctrl.add_slot(0)


async def test_add_slot_no_browser_raises(monkeypatch) -> None:
    broker = Broker()
    ctrl = PoolController(broker, cdp_base=9222)

    async def fake_probe(port, *, timeout=1.0):
        return None

    monkeypatch.setattr(pool_mod, "probe", fake_probe)
    with pytest.raises(NoBrowserOnPort):
        await ctrl.add_slot(5)


async def test_add_slot_registers_and_holds_lock(monkeypatch) -> None:
    broker = Broker()
    ctrl = PoolController(broker, cdp_base=9222)
    fake_lease = _FakeLease()

    async def fake_probe(port, *, timeout=1.0):
        return {"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/x", "Browser": "Chrome/149"}

    async def fake_register(broker_, browser, *, tier="autonomous"):
        rec = broker_.register(
            slot=browser.slot, cdp_port=browser.cdp_port, cdp_ws_url=browser.cdp_ws_url, pid=-1
        )
        return SlotRegistration(lease=fake_lease, record=rec, stale_closed=0)

    monkeypatch.setattr(pool_mod, "probe", fake_probe)
    monkeypatch.setattr(pool_mod, "register_browser", fake_register)

    rec = await ctrl.add_slot(6)
    assert rec.slot == 6
    assert broker.has_slot(6)
    assert ctrl.locks[6] is fake_lease


# --------------------------------------------------------------------------- #
# PoolController.remove_slot                                                  #
# --------------------------------------------------------------------------- #
async def test_remove_slot_unregisters_and_releases_lock() -> None:
    broker = Broker()
    _seed(broker, 2)
    ctrl = PoolController(broker, cdp_base=9222)
    fake_lease = _FakeLease()
    ctrl.adopt(1, fake_lease)

    rec = await ctrl.remove_slot(1)
    assert rec.slot == 1
    assert not broker.has_slot(1)
    assert fake_lease.released is True
    assert 1 not in ctrl.locks


async def test_remove_leased_slot_requires_force() -> None:
    broker = Broker()
    _seed(broker, 2)
    ctrl = PoolController(broker, cdp_base=9222)
    ctrl.adopt(0, _FakeLease())
    await broker.acquire(holder="A", slot=0)
    with pytest.raises(SlotLeased):
        await ctrl.remove_slot(0)
    assert broker.has_slot(0)


async def test_release_all_releases_every_lock() -> None:
    broker = Broker()
    _seed(broker, 2)
    ctrl = PoolController(broker, cdp_base=9222)
    leases = [_FakeLease(), _FakeLease()]
    ctrl.adopt(0, leases[0])
    ctrl.adopt(1, leases[1])
    ctrl.release_all()
    assert all(lease.released for lease in leases)
    assert ctrl.locks == {}
