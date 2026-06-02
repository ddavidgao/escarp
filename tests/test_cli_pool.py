"""`escarp pool add/remove` flow: launch/register on add, unbroker/kill on
remove, the no-broker and leased guards, and size persistence."""

from __future__ import annotations

from pathlib import Path

import escarp.cli_pool as cp
from escarp.pool_config import PoolConfig


class FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)

    def json(self) -> dict:
        return self._payload


def _patch(monkeypatch, *, broker_port=7878, present=(0, 1, 2, 3, 4, 5), post_status=200):
    calls = {"post": [], "launched": [], "terminate": [], "remove_data": [], "saved": []}

    monkeypatch.setattr(cp, "find_broker_port", lambda: broker_port)
    monkeypatch.setattr(
        cp, "broker_status", lambda port: {"slots": [{"slot": s} for s in present]}
    )
    monkeypatch.setattr(
        cp, "load_pool_config", lambda path=None: PoolConfig(cdp_base=9222, cua_apps=True, cft_binary="/x")
    )
    monkeypatch.setattr(cp, "resolve_cft_binary", lambda cfg: Path("/fake/cft"))

    async def fake_ensure(*, slot, cft_binary, cdp_base_port, tier, cua_apps):
        calls["launched"].append(slot)
        return True

    def fake_post(url, json=None, timeout=None):
        calls["post"].append((url, json))
        return FakeResp(post_status, {"slot": json.get("slot")})

    monkeypatch.setattr(cp, "ensure_slot_chrome", fake_ensure)
    monkeypatch.setattr(cp.httpx, "post", fake_post)
    monkeypatch.setattr(cp, "terminate_chrome_on_port", lambda port, **k: calls["terminate"].append(port) or True)
    monkeypatch.setattr(cp, "remove_slot_data", lambda slot, **k: calls["remove_data"].append(slot))
    monkeypatch.setattr(cp, "save_pool_config", lambda cfg, path=None: calls["saved"].append(cfg))
    return calls


# ----- add ----------------------------------------------------------------- #
def test_add_defaults_to_lowest_free_and_launches(monkeypatch) -> None:
    calls = _patch(monkeypatch, present=(0, 1, 2, 3, 4, 5))
    rc = cp.main(["add", "--cua-apps"])
    assert rc == 0
    assert calls["launched"] == [6]
    assert calls["post"][0][0].endswith("/pool/add")
    assert calls["post"][0][1] == {"slot": 6}
    assert calls["saved"][0].pool_size == 7  # max(0..6)+1


def test_add_no_launch_skips_chrome(monkeypatch) -> None:
    calls = _patch(monkeypatch, present=(0, 1))
    rc = cp.main(["add", "5", "--no-launch"])
    assert rc == 0
    assert calls["launched"] == []
    assert calls["post"][0][1] == {"slot": 5}


def test_add_existing_slot_is_noop(monkeypatch) -> None:
    calls = _patch(monkeypatch, present=(0, 1, 2))
    rc = cp.main(["add", "1"])
    assert rc == 0
    assert calls["post"] == []


def test_add_without_broker_fails(monkeypatch) -> None:
    _patch(monkeypatch, broker_port=None)
    assert cp.main(["add"]) == 4


def test_add_missing_binary_fails(monkeypatch) -> None:
    _patch(monkeypatch, present=(0,))
    monkeypatch.setattr(cp, "resolve_cft_binary", lambda cfg: None)
    assert cp.main(["add", "3", "--cua-apps"]) == 2


def test_add_broker_rejects_propagates(monkeypatch) -> None:
    _patch(monkeypatch, present=(0,), post_status=409)
    assert cp.main(["add", "3", "--no-launch"]) == 1


# ----- remove -------------------------------------------------------------- #
def test_remove_success_kills_chrome_and_cleans(monkeypatch) -> None:
    calls = _patch(monkeypatch, present=(0, 1, 2, 3, 4))
    rc = cp.main(["remove", "5"])
    assert rc == 0
    assert calls["post"][0][0].endswith("/pool/remove")
    assert calls["terminate"] == [9227]
    assert calls["remove_data"] == [5]
    assert calls["saved"][0].pool_size == 5  # max(0..4)+1


def test_remove_keep_flags_preserve_chrome_and_data(monkeypatch) -> None:
    calls = _patch(monkeypatch, present=(0, 1, 2, 3, 4))
    rc = cp.main(["remove", "5", "--keep-chrome", "--keep-data"])
    assert rc == 0
    assert calls["terminate"] == []
    assert calls["remove_data"] == []


def test_remove_leased_without_force_fails(monkeypatch) -> None:
    calls = _patch(monkeypatch, post_status=409)
    rc = cp.main(["remove", "5"])
    assert rc == 3
    assert calls["terminate"] == []


def test_remove_unknown_slot_fails(monkeypatch) -> None:
    calls = _patch(monkeypatch, post_status=404)
    rc = cp.main(["remove", "9"])
    assert rc == 1
    assert calls["terminate"] == []


def test_remove_without_broker_fails(monkeypatch) -> None:
    _patch(monkeypatch, broker_port=None)
    assert cp.main(["remove", "5"]) == 4
