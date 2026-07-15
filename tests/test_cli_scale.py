"""`escarp scale` reconcile logic.

These tests mock the side-effecting layer (port scan, chrome launch/kill, daemon
restart) so we verify only the planning + safety policy: what gets launched,
what gets removed, when we refuse, and that nothing happens on --dry-run.
"""

from __future__ import annotations

from pathlib import Path

import escarp.cli_scale as cs
from escarp.broker.procs import ChromeProc
from escarp.pool_config import PoolConfig


def _patch_common(
    monkeypatch, *, live, leased=None, restart_rc=0, procs=None, daemon_pid=4242, confirm_live=None
):
    calls = {
        "launch": [],
        "terminate": [],
        "remove_data": [],
        "restart": 0,
        "saved": [],
        "reap": [],
        "stop": [],
    }
    # The sweep-grade confirmation re-probe agrees with the fast scan unless a
    # test says otherwise (confirm_live simulates a busy chrome that missed
    # the fast probe but answers the longer one).
    confirm = set(live) if confirm_live is None else set(confirm_live)

    async def fake_scan(cdp_base, upper, *, timeout=0.3):
        return set(live)

    async def fake_probe(port, *, timeout=2.0):
        return {"Browser": "x"} if (port - 9222) in confirm else None

    monkeypatch.setattr(cs, "probe", fake_probe)

    async def fake_launch(*, pool_size, cdp_base_port, cft_binary, cua_apps):
        calls["launch"].append((pool_size, cdp_base_port, cua_apps))
        return 0

    def fake_restart():
        calls["restart"] += 1
        return restart_rc

    monkeypatch.setattr(cs, "_scan_live_slots", fake_scan)
    monkeypatch.setattr(cs, "scan_escarp_chromes", lambda **k: list(procs or []))
    monkeypatch.setattr(cs, "terminate_pids", lambda pids, **k: calls["reap"].append(list(pids)) or list(pids))
    monkeypatch.setattr(cs, "launch_pool", fake_launch)
    monkeypatch.setattr(cs, "resolve_cft_binary", lambda cfg: Path("/fake/cft"))
    monkeypatch.setattr(
        cs,
        "terminate_slot_chromes",
        lambda slot, port, **k: (calls["terminate"].append(port) or True),
    )
    monkeypatch.setattr(cs, "remove_slot_data", lambda slot, **k: calls["remove_data"].append(slot))
    monkeypatch.setattr(cs, "_leased_slots_among", lambda slots: list(leased or []))
    monkeypatch.setattr(cs, "_restart_daemon", fake_restart)
    monkeypatch.setattr(cs, "find_daemon_pid", lambda: daemon_pid)
    monkeypatch.setattr(cs, "stop_daemon", lambda pid, **k: calls["stop"].append(pid) or True)
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


def test_size_must_be_nonnegative(monkeypatch) -> None:
    assert cs.main(["-1"]) == 2


def test_scale_zero_tears_down_pool_and_stops_daemon(monkeypatch) -> None:
    calls = _patch_common(monkeypatch, live={0, 1})
    rc = cs.main(["0", "--cua-apps"])
    assert rc == 0
    assert calls["launch"] == []
    assert sorted(calls["terminate"]) == [9222, 9223]
    assert sorted(calls["remove_data"]) == [0, 1]
    assert calls["saved"][0].pool_size == 0
    assert calls["stop"] == [4242]  # daemon stopped, not restarted
    assert calls["restart"] == 0


def test_scale_zero_without_daemon_still_succeeds(monkeypatch) -> None:
    calls = _patch_common(monkeypatch, live={0}, daemon_pid=None)
    rc = cs.main(["0", "--cua-apps"])
    assert rc == 0
    assert calls["terminate"] == [9222]
    assert calls["stop"] == []


def test_scale_zero_refuses_leased_without_force(monkeypatch) -> None:
    calls = _patch_common(monkeypatch, live={0, 1}, leased=[1])
    assert cs.main(["0", "--cua-apps"]) == 3
    assert calls["terminate"] == []
    assert calls["stop"] == []


def test_reaps_cdp_dead_chromes_on_scale_down(monkeypatch) -> None:
    # Slots 0-1 answer CDP; pids 900/901 are escarp chromes with no listener
    # (crashed or beyond the scan window). They must die even though the port
    # scan can't see them -- this is the accumulation bug.
    calls = _patch_common(
        monkeypatch,
        live={0, 1},
        procs=[
            ChromeProc(pid=900, slot=17, command="c"),
            ChromeProc(pid=901, slot=None, command="c"),
            ChromeProc(pid=902, slot=0, command="c"),  # healthy keeper: untouched
        ],
    )
    rc = cs.main(["1", "--cua-apps"])
    assert rc == 0
    assert calls["reap"] == [[900, 901]]
    assert 17 in calls["remove_data"]  # dead slot's data cleaned
    assert calls["terminate"] == [9223]  # live slot 1 removed via port kill


def test_reaps_dead_keeper_slot_before_relaunch(monkeypatch) -> None:
    # Slot 1 is below target but CDP-dead: kill the corpse first, then the
    # launch step refills it. Its profile must survive for the relaunch.
    calls = _patch_common(
        monkeypatch,
        live={0},
        procs=[ChromeProc(pid=910, slot=1, command="c")],
    )
    rc = cs.main(["2", "--cua-apps"])
    assert rc == 0
    assert calls["reap"] == [[910]]
    assert calls["remove_data"] == []
    assert calls["launch"] == [(2, 9222, True)]


def test_dry_run_skips_dead_proc_reap(monkeypatch) -> None:
    calls = _patch_common(monkeypatch, live={0}, procs=[ChromeProc(pid=920, slot=9, command="c")])
    assert cs.main(["1", "--cua-apps", "--dry-run"]) == 0
    assert calls["reap"] == []


def test_leased_dead_proc_refused_without_force(monkeypatch) -> None:
    # A leased slot whose chrome missed the probes must not be reaped without
    # --force: killing it yanks the browser out from under a working agent.
    calls = _patch_common(
        monkeypatch,
        live={0},
        leased=[3],
        procs=[ChromeProc(pid=950, slot=3, command="c")],
    )
    assert cs.main(["1", "--cua-apps"]) == 3
    assert calls["reap"] == []
    assert calls["terminate"] == []


def test_force_reaps_leased_dead_proc(monkeypatch) -> None:
    calls = _patch_common(
        monkeypatch,
        live={0},
        leased=[3],
        procs=[ChromeProc(pid=950, slot=3, command="c")],
    )
    assert cs.main(["1", "--cua-apps", "--force"]) == 0
    assert calls["reap"] == [[950]]


def test_confirm_probe_revives_fast_scan_miss(monkeypatch) -> None:
    # The fast 0.3s scan missed slot 3 but the sweep-grade re-probe answers:
    # the chrome is alive, so it leaves via the guarded port-removal path
    # (slot >= target), never the dead-proc reap.
    calls = _patch_common(
        monkeypatch,
        live={0},
        confirm_live={0, 3},
        procs=[ChromeProc(pid=960, slot=3, command="c")],
    )
    assert cs.main(["1", "--cua-apps"]) == 0
    assert calls["reap"] == []
    assert calls["terminate"] == [9225]


def test_scale_zero_no_restart_leaves_daemon_alone(monkeypatch) -> None:
    calls = _patch_common(monkeypatch, live={0, 1})
    assert cs.main(["0", "--cua-apps", "--no-restart"]) == 0
    assert sorted(calls["terminate"]) == [9222, 9223]
    assert calls["saved"][0].pool_size == 0
    assert calls["stop"] == []
    assert calls["restart"] == 0
