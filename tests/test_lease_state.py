"""Local lease-state file: round-trips + slot-uniqueness semantics."""

from __future__ import annotations

import json
from pathlib import Path

from escarp.lease_state import (
    LocalLease,
    add,
    all_leases,
    find_by_holder,
    find_by_slot,
    make,
    remove_by_slot,
    remove_by_token,
)


def _sample(slot: int = 0, holder: str = "demo", token: str = "tok-abc") -> LocalLease:
    return LocalLease(
        slot=slot,
        holder=holder,
        lease_token=token,
        cdp_port=9222 + slot,
        cdp_ws_url=f"ws://127.0.0.1:{9222 + slot}/devtools/browser/x",
        acquired_at=1234567890.0,
    )


def test_add_and_read_round_trip(tmp_path: Path) -> None:
    f = tmp_path / "leases.json"
    add(_sample(0), path=f)
    add(_sample(1, token="tok-2"), path=f)

    leases = all_leases(path=f)
    assert {l.slot for l in leases} == {0, 1}


def test_add_replaces_existing_slot(tmp_path: Path) -> None:
    """Broker has one lease per slot; local state must mirror that."""
    f = tmp_path / "leases.json"
    add(_sample(0, token="old"), path=f)
    add(_sample(0, token="new"), path=f)

    leases = all_leases(path=f)
    assert len(leases) == 1
    assert leases[0].lease_token == "new"


def test_remove_by_slot(tmp_path: Path) -> None:
    f = tmp_path / "leases.json"
    add(_sample(0), path=f)
    add(_sample(1, token="tok-2"), path=f)
    removed = remove_by_slot(0, path=f)
    assert removed is not None and removed.slot == 0
    assert {l.slot for l in all_leases(path=f)} == {1}


def test_remove_by_slot_missing_returns_none(tmp_path: Path) -> None:
    f = tmp_path / "leases.json"
    assert remove_by_slot(99, path=f) is None


def test_remove_by_token(tmp_path: Path) -> None:
    f = tmp_path / "leases.json"
    add(_sample(0, token="tok-A"), path=f)
    add(_sample(1, token="tok-B"), path=f)
    assert remove_by_token("tok-A", path=f) is True
    assert remove_by_token("tok-A", path=f) is False  # idempotent


def test_find_by_slot_and_holder(tmp_path: Path) -> None:
    f = tmp_path / "leases.json"
    add(_sample(0, holder="alice"), path=f)
    add(_sample(1, holder="bob", token="tok-2"), path=f)
    add(_sample(2, holder="alice", token="tok-3"), path=f)

    assert find_by_slot(0, path=f).holder == "alice"
    assert find_by_slot(99, path=f) is None
    alices = find_by_holder("alice", path=f)
    assert {l.slot for l in alices} == {0, 2}


def test_corrupt_file_is_treated_as_empty(tmp_path: Path) -> None:
    f = tmp_path / "leases.json"
    f.write_text("not json")
    assert all_leases(path=f) == []


def test_make_from_broker_response() -> None:
    record = {
        "slot": 3,
        "holder": "agent-x",
        "lease_token": "tok-xyz",
        "cdp_port": 9225,
        "cdp_ws_url": "ws://127.0.0.1:9225/devtools/browser/abc",
        "acquired_at": 100.0,
    }
    l = make(record)
    assert l.slot == 3
    assert l.holder == "agent-x"
    assert l.lease_token == "tok-xyz"


def test_atomic_file_contents_match_json(tmp_path: Path) -> None:
    f = tmp_path / "leases.json"
    add(_sample(0), path=f)
    data = json.loads(f.read_text())
    assert isinstance(data, list)
    assert data[0]["slot"] == 0
