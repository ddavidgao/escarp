"""Lease broker logic: state transitions, expiry, reaper, concurrency."""

from __future__ import annotations

import asyncio

import pytest

from escarp.broker.lease import (
    Broker,
    PoolExhausted,
    SlotLeased,
    UnknownLease,
    UnknownSlot,
)


def _seed(broker: Broker, n: int = 2) -> None:
    for slot in range(n):
        broker.register(
            slot=slot,
            cdp_port=9222 + slot,
            cdp_ws_url=f"ws://127.0.0.1:{9222 + slot}/devtools/browser/seed-{slot}",
            pid=10000 + slot,
        )


async def test_acquire_any_picks_lowest_free() -> None:
    broker = Broker()
    _seed(broker, 3)
    rec = await broker.acquire(holder="A")
    assert rec.slot == 0
    assert rec.state == "leased"
    assert rec.holder == "A"
    assert rec.lease_token is not None


async def test_acquire_specific_slot() -> None:
    broker = Broker()
    _seed(broker, 3)
    rec = await broker.acquire(holder="B", slot=2)
    assert rec.slot == 2
    assert rec.state == "leased"


async def test_acquire_slot_already_leased() -> None:
    broker = Broker()
    _seed(broker, 2)
    await broker.acquire(holder="A", slot=0)
    with pytest.raises(SlotLeased):
        await broker.acquire(holder="B", slot=0)


async def test_acquire_unknown_slot() -> None:
    broker = Broker()
    _seed(broker, 1)
    with pytest.raises(UnknownSlot):
        await broker.acquire(holder="A", slot=42)


async def test_acquire_pool_exhausted() -> None:
    broker = Broker()
    _seed(broker, 1)
    await broker.acquire(holder="A")
    with pytest.raises(PoolExhausted):
        await broker.acquire(holder="B")


async def test_release_frees_slot() -> None:
    broker = Broker()
    _seed(broker, 1)
    rec = await broker.acquire(holder="A")
    token = rec.lease_token
    assert token is not None
    released = await broker.release(lease_token=token)
    assert released.state == "free"
    assert released.holder is None
    # immediately re-acquirable
    re_acquired = await broker.acquire(holder="B")
    assert re_acquired.slot == 0
    assert re_acquired.holder == "B"


async def test_release_unknown_lease() -> None:
    broker = Broker()
    _seed(broker, 1)
    with pytest.raises(UnknownLease):
        await broker.release(lease_token="garbage")


async def test_heartbeat_extends_expiry() -> None:
    broker = Broker(lease_ttl_s=10.0)
    _seed(broker, 1)
    rec = await broker.acquire(holder="A")
    first_expiry = rec.expires_at
    token = rec.lease_token
    assert token is not None
    await asyncio.sleep(0.05)
    refreshed = await broker.heartbeat(lease_token=token)
    assert refreshed.expires_at is not None
    assert first_expiry is not None
    assert refreshed.expires_at > first_expiry


async def test_reaper_frees_expired_leases() -> None:
    broker = Broker(lease_ttl_s=0.05)
    _seed(broker, 2)
    await broker.acquire(holder="A", slot=0)
    await broker.acquire(holder="B", slot=1)
    snapshot = broker.snapshot()
    assert all(s["state"] == "leased" for s in snapshot)

    await asyncio.sleep(0.1)
    reaped = await broker.reap()
    assert sorted(reaped) == [0, 1]
    snapshot = broker.snapshot()
    assert all(s["state"] == "free" for s in snapshot)
    assert broker.reaped_log()  # log has entries


async def test_reaper_no_op_when_fresh() -> None:
    broker = Broker(lease_ttl_s=10.0)
    _seed(broker, 1)
    await broker.acquire(holder="A")
    reaped = await broker.reap()
    assert reaped == []


async def test_snapshot_does_not_leak_token() -> None:
    broker = Broker()
    _seed(broker, 1)
    rec = await broker.acquire(holder="A")
    assert rec.lease_token  # holder DOES get it from acquire()
    snapshot = broker.snapshot()
    assert "lease_token" not in snapshot[0]


async def test_snapshot_surfaces_bounded_wait_fields() -> None:
    broker = Broker(lease_ttl_s=10.0, reaper_interval_s=2.0)
    _seed(broker, 2)
    rec = await broker.acquire(holder="A", slot=0)
    assert rec.last_heartbeat is not None
    rec.last_heartbeat -= 6.0
    rec.expires_at = rec.last_heartbeat + broker.lease_ttl_s

    snapshot = broker.snapshot()
    leased = next(s for s in snapshot if s["slot"] == 0)
    free = next(s for s in snapshot if s["slot"] == 1)

    assert leased["suspected_stale"] is True
    assert 5.0 < leased["available_after_s"] <= 6.0
    assert leased["retry_after_s"] == leased["available_after_s"]
    assert free["suspected_stale"] is False
    assert free["available_after_s"] == 0.0
    assert free["retry_after_s"] == 0.0


async def test_concurrent_acquires_serialize() -> None:
    """Two concurrent acquires on the same single-slot pool: exactly one wins."""
    broker = Broker()
    _seed(broker, 1)

    results = await asyncio.gather(
        broker.acquire(holder="A"),
        broker.acquire(holder="B"),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], PoolExhausted)


async def test_reset_fn_called_on_release() -> None:
    """The state-reset hook must fire at the lease boundary -- David's
    'no state inheritance' constraint."""
    calls: list[int] = []

    async def fake_reset(cdp_port: int) -> None:
        calls.append(cdp_port)

    broker = Broker(reset_fn=fake_reset)
    _seed(broker, 2)
    rec = await broker.acquire(holder="A", slot=1)
    assert rec.lease_token is not None
    assert calls == []  # acquire does not reset
    await broker.release(lease_token=rec.lease_token)
    assert calls == [9223]  # slot 1 -> cdp_port 9223


async def test_reset_fn_called_for_each_reaped_slot() -> None:
    calls: list[int] = []

    async def fake_reset(cdp_port: int) -> None:
        calls.append(cdp_port)

    broker = Broker(lease_ttl_s=0.05, reset_fn=fake_reset)
    _seed(broker, 2)
    await broker.acquire(holder="A", slot=0)
    await broker.acquire(holder="B", slot=1)
    await asyncio.sleep(0.1)
    reaped = await broker.reap()
    assert sorted(reaped) == [0, 1]
    assert sorted(calls) == [9222, 9223]


async def test_reset_fn_exception_does_not_block_release() -> None:
    async def angry_reset(cdp_port: int) -> None:
        raise RuntimeError("chrome is on fire")

    broker = Broker(reset_fn=angry_reset)
    _seed(broker, 1)
    rec = await broker.acquire(holder="A")
    assert rec.lease_token is not None
    released = await broker.release(lease_token=rec.lease_token)
    assert released.state == "free"
    # slot is reclaimable even though reset blew up
    re_acquired = await broker.acquire(holder="B")
    assert re_acquired.slot == 0
