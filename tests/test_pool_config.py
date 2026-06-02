"""Pool config persistence: defaults, round-trip, and corruption tolerance."""

from __future__ import annotations

from pathlib import Path

from escarp.pool_config import PoolConfig, load_pool_config, save_pool_config


def test_load_missing_returns_defaults(tmp_path: Path) -> None:
    cfg = load_pool_config(tmp_path / "nope.json")
    assert cfg == PoolConfig()
    assert cfg.pool_size == 4
    assert cfg.cdp_base == 9222
    assert cfg.cua_apps is False


def test_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "pool.json"
    save_pool_config(PoolConfig(pool_size=6, cdp_base=9222, cua_apps=True), p)
    cfg = load_pool_config(p)
    assert cfg.pool_size == 6
    assert cfg.cua_apps is True


def test_corrupt_file_returns_defaults(tmp_path: Path) -> None:
    p = tmp_path / "pool.json"
    p.write_text("{ not valid json")
    assert load_pool_config(p) == PoolConfig()


def test_non_dict_json_returns_defaults(tmp_path: Path) -> None:
    p = tmp_path / "pool.json"
    p.write_text("[1, 2, 3]")
    assert load_pool_config(p) == PoolConfig()


def test_partial_fields_are_filled_from_defaults(tmp_path: Path) -> None:
    p = tmp_path / "pool.json"
    p.write_text('{"pool_size": 8}')
    cfg = load_pool_config(p)
    assert cfg.pool_size == 8
    assert cfg.cdp_base == 9222  # default filled in
    assert cfg.tier == "autonomous"


def test_save_is_atomic_no_tmp_left_behind(tmp_path: Path) -> None:
    p = tmp_path / "pool.json"
    save_pool_config(PoolConfig(pool_size=5), p)
    assert p.exists()
    assert not (tmp_path / "pool.json.tmp").exists()
