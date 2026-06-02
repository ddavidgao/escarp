"""HTTP error mapping for the /pool/add and /pool/remove endpoints."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from escarp.broker.api import build_app
from escarp.broker.lease import Broker, LeaseRecord, SlotLeased, UnknownSlot
from escarp.broker.pool import NoBrowserOnPort, SlotAlreadyInPool


class StubPool:
    def __init__(self, *, on_add=None, on_remove=None) -> None:
        self._on_add = on_add
        self._on_remove = on_remove

    async def add_slot(self, slot: int):
        return self._on_add(slot)

    async def remove_slot(self, slot: int, *, force: bool = False):
        return self._on_remove(slot, force)


@pytest.fixture
async def make_client():
    clients: list[TestClient] = []

    async def _make(pool) -> TestClient:
        client = TestClient(TestServer(build_app(Broker(), pool=pool)))
        await client.start_server()
        clients.append(client)
        return client

    yield _make
    for c in clients:
        await c.close()


# ----- add ----------------------------------------------------------------- #
async def test_pool_add_success(make_client) -> None:
    rec = LeaseRecord(slot=6, cdp_port=9228, cdp_ws_url="ws://x", pid=-1)
    client = await make_client(StubPool(on_add=lambda slot: rec))
    resp = await client.post("/pool/add", json={"slot": 6})
    assert resp.status == 200
    assert (await resp.json())["slot"] == 6


async def test_pool_add_unavailable_without_controller(make_client) -> None:
    client = await make_client(None)
    resp = await client.post("/pool/add", json={"slot": 0})
    assert resp.status == 503


async def test_pool_add_missing_slot_is_400(make_client) -> None:
    client = await make_client(StubPool(on_add=lambda slot: None))
    resp = await client.post("/pool/add", json={})
    assert resp.status == 400


async def test_pool_add_already_in_pool_is_409(make_client) -> None:
    def boom(slot):
        raise SlotAlreadyInPool("already")

    client = await make_client(StubPool(on_add=boom))
    resp = await client.post("/pool/add", json={"slot": 0})
    assert resp.status == 409
    assert (await resp.json())["error"] == "slot_already_in_pool"


async def test_pool_add_no_browser_is_409(make_client) -> None:
    def boom(slot):
        raise NoBrowserOnPort("nothing on port")

    client = await make_client(StubPool(on_add=boom))
    resp = await client.post("/pool/add", json={"slot": 9})
    assert resp.status == 409
    assert (await resp.json())["error"] == "no_browser_on_port"


# ----- remove -------------------------------------------------------------- #
async def test_pool_remove_success(make_client) -> None:
    rec = LeaseRecord(slot=7, cdp_port=9229, cdp_ws_url="ws://y", pid=-1)
    client = await make_client(StubPool(on_remove=lambda slot, force: rec))
    resp = await client.post("/pool/remove", json={"slot": 7})
    assert resp.status == 200
    body = await resp.json()
    assert body["removed"] is True
    assert body["slot"] == 7


async def test_pool_remove_unknown_is_404(make_client) -> None:
    def boom(slot, force):
        raise UnknownSlot("nope")

    client = await make_client(StubPool(on_remove=boom))
    resp = await client.post("/pool/remove", json={"slot": 42})
    assert resp.status == 404


async def test_pool_remove_leased_is_409(make_client) -> None:
    def boom(slot, force):
        raise SlotLeased("held")

    client = await make_client(StubPool(on_remove=boom))
    resp = await client.post("/pool/remove", json={"slot": 0})
    assert resp.status == 409


async def test_pool_remove_force_passed_through(make_client) -> None:
    seen = {}

    def ok(slot, force):
        seen["force"] = force
        return LeaseRecord(slot=slot, cdp_port=9222 + slot, cdp_ws_url="ws://z", pid=-1)

    client = await make_client(StubPool(on_remove=ok))
    resp = await client.post("/pool/remove", json={"slot": 1, "force": True})
    assert resp.status == 200
    assert seen["force"] is True
