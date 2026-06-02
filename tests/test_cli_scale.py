"""`escarp scale` reconcile logic.

These tests mock the side-effecting layer (port scan, chrome launch/kill, daemon
restart) so we verify only the planning + safety policy: what gets launched,
what gets removed, when we refuse, and that nothing happens on --dry-run.
"""

from __future__ import annotations

from pathlib import Path

import escarp.cli_scale as cs
from escarp.pool_config import PoolConfig


def _patch_common(monkeypatch, *, live, leased=None, restart_rc=0):
    calls = {"launch": [], "terminate": [], "remove_data": [], "restart": 0, "saved": []}

    async def fake_scan(cdp_base, upper, *, timeout=0.3):
        return set(live)

    async def fake_launch(*, pool_size, cdp_base_port, cft_binary, cua_apps):
        calls["launch"].append((pool_size, cdp_base_port, cua_apps))
        return 0

    def fake_restart():
        calls["restart"] += 1
        return restart_rc

    monkeypatch.setattr(cs, "_scan_live_slots", fake_scan)
    monkeypatch.setattr(cs, "launch_pool", fake_launch)
    monkeypatch.setattr(cs, "find_cft_binary", lambda: Path("/fake/cft"))
    monkeypatch.setattr(
        cs, "_terminate_chrome_on_port", lambda port, **k: (calls["terminate"].append(port) or True)
    )
    monkeypatch.setattr(cs, "_remove_slot_data", lambda slot, **k: calls["remove_data"].append(slot))
    monkeypatch.setattr(cs, "_leased_slots_among", lambda slots: list(leased or []))
    monkeypatch.setattr(cs, "_restart_daemon", fake_restart)
    monkeypatch.setattr(cs, "save_pool_config", lambda cfg, path=None: calls["saved"].append(cfg))
    monkeypatch.setattr(
        cs, "load_pool_config", lambda path=None: PoolConfig(pool_size=4, cdp_base=9222, cua_apps=True)
    )
    return calls


def test_scale_up_launches_gap_and_restarts(monkeypatch) -> None:
    calls = _patch_common(monkeypatch, live={0, 1, 2, 3})
    rc = cs.main(["6", "--cua-apps"])
    assert rc == 0
    assert calls["launch"] == [(6, 9222, True)]
    assert calls["terminate"] == []
    assert calls["restart"] == 1
    assert calls["saved"][0].pool_size == 6


def test_scale_down_terminates_excess_and_cleans(monkeypatch) -> None:
    calls = _patch_common(monkeypatch, live={0, 1, 2, 3, 4, 5})
    rc = cs.main(["4", "--cua-apps"])
    assert rc == 0
    assert calls["launch"] == []  # nothing below target was missing
    assert sorted(calls["terminate"]) == [9226, 9227]
    assert sorted(calls["remove_data"]) == [4, 5]
    assert calls["restart"] == 1
    assert calls["saved"][0].pool_size == 4


def test_dry_run_changes_nothing(monkeypatch) -> None:
    calls = _patch_common(monkeypatch, live={0, 1, 2, 3, 4, 5})
    rc = cs.main(["4", "--cua-apps", "--dry-run"])
    assert rc == 0
    assert calls["launch"] == []
    assert calls["terminate"] == []
    assert calls["remove_data"] == []
    assert calls["restart"] == 0
    assert calls["saved"] == []


def test_refuses_to_remove_leased_slot_without_force(monkeypatch) -> None:
    calls = _patch_common(monkeypatch, live={0, 1, 2, 3, 4, 5}, leased=[5])
    rc = cs.main(["4", "--cua-apps"])
    assert rc == 3
    assert calls["terminate"] == []
    assert calls["restart"] == 0


def test_force_removes_leased_slot(monkeypatch) -> None:
    calls = _patch_common(monkeypatch, live={0, 1, 2, 3, 4, 5}, leased=[5])
    rc = cs.main(["4", "--cua-apps", "--force"])
    assert rc == 0
    assert sorted(calls["terminate"]) == [9226, 9227]
    assert calls["restart"] == 1


def test_keep_data_skips_cleanup(monkeypatch) -> None:
    calls = _patch_common(monkeypatch, live={0, 1, 2, 3, 4, 5})
    rc = cs.main(["4", "--cua-apps", "--keep-data"])
    assert rc == 0
    assert sorted(calls["terminate"]) == [9226, 9227]
    assert calls["remove_data"] == []


def test_no_restart_reconciles_and_persists_only(monkeypatch) -> None:
    calls = _patch_common(monkeypatch, live={0, 1, 2, 3})
    rc = cs.main(["6", "--cua-apps", "--no-restart"])
    assert rc == 0
    assert calls["launch"] == [(6, 9222, True)]
    assert calls["restart"] == 0
    assert calls["saved"][0].pool_size == 6


def test_fills_interior_gap_on_scale_up(monkeypatch) -> None:
    # slot 4 is missing below target 6; launch_pool is idempotent so we still
    # call it at the full target and let it skip the live ones.
    calls = _patch_common(monkeypatch, live={0, 1, 2, 3, 5})
    rc = cs.main(["6", "--cua-apps"])
    assert rc == 0
    assert calls["launch"] == [(6, 9222, True)]
    assert calls["terminate"] == []  # slot 5 is below target, not removed


def test_size_must_be_positive(monkeypatch) -> None:
    assert cs.main(["0"]) == 2
