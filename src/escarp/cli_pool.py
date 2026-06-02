"""`escarp pool add/remove` -- change pool membership on a RUNNING daemon, with
no restart.

Unlike `escarp scale` (which reconciles + bounces the daemon), these talk to the
broker's hot-pool endpoints so the change is live:

  add:    ensure a chrome is listening on the slot (launch it if needed), then
          POST /pool/add so the running broker discovers + brokers it.
  remove: POST /pool/remove so the broker unbrokers + drops the slot lock, then
          terminate the chrome and clean its data here (the daemon never kills
          chromes itself).

Both then persist the resulting size to pool.json so a future daemon restart
re-reads it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from escarp.broker.launcher import ensure_slot_chrome
from escarp.pool_config import DEFAULT_CONFIG_PATH, PoolConfig, load_pool_config, save_pool_config
from escarp.slot_ops import (
    broker_status,
    find_broker_port,
    remove_slot_data,
    resolve_cft_binary,
    resolve_cua_apps,
    terminate_chrome_on_port,
)


def _current_slots(api_port: int) -> set[int]:
    status = broker_status(api_port) or {}
    return {s["slot"] for s in status.get("slots", [])}


def _lowest_free(present: set[int]) -> int:
    slot = 0
    while slot in present:
        slot += 1
    return slot


def _persist_size(cfg: PoolConfig, *, cdp_base: int, cua_apps: bool, slots: set[int]) -> None:
    new_size = (max(slots) + 1) if slots else 0
    save_pool_config(
        PoolConfig(
            pool_size=new_size,
            cdp_base=cdp_base,
            tier=cfg.tier,
            cua_apps=cua_apps,
            cft_binary=cfg.cft_binary,
        )
    )
    print(f"persisted pool_size={new_size} to {DEFAULT_CONFIG_PATH}")


def _add(args: argparse.Namespace) -> int:
    api_port = find_broker_port()
    if api_port is None:
        print("no running broker found. Start it with `escarp daemon`.", file=sys.stderr)
        return 4

    cfg = load_pool_config()
    cdp_base = args.cdp_base if args.cdp_base is not None else cfg.cdp_base
    cua_apps = resolve_cua_apps(args.cua_apps, cfg)

    present = _current_slots(api_port)
    slot = args.slot if args.slot is not None else _lowest_free(present)
    if slot in present:
        print(f"slot {slot} is already in the pool.")
        return 0

    if not args.no_launch:
        cft = resolve_cft_binary(cfg)
        if cft is None:
            print(
                "Chrome for Testing binary not found; cannot launch the slot.\n"
                "Set ESCARP_CFT_BINARY, run `npx @puppeteer/browsers install chrome@stable`, "
                "or pass --no-launch if a chrome is already listening.",
                file=sys.stderr,
            )
            return 2
        launched = asyncio.run(
            ensure_slot_chrome(
                slot=slot,
                cft_binary=cft,
                cdp_base_port=cdp_base,
                tier=cfg.tier,
                cua_apps=cua_apps,
            )
        )
        print(
            f"[slot {slot}] chrome {'launched' if launched else 'already up'} "
            f"on cdp_port {cdp_base + slot}"
        )

    try:
        resp = httpx.post(
            f"http://127.0.0.1:{api_port}/pool/add", json={"slot": slot}, timeout=20.0
        )
    except httpx.HTTPError as exc:
        print(f"add failed: {exc}", file=sys.stderr)
        return 1
    if resp.status_code != 200:
        print(f"add failed: HTTP {resp.status_code}: {resp.text}", file=sys.stderr)
        return 1

    print(f"[slot {slot}] added to the live pool (no daemon restart).")
    _persist_size(cfg, cdp_base=cdp_base, cua_apps=cua_apps, slots=present | {slot})
    return 0


def _remove(args: argparse.Namespace) -> int:
    api_port = find_broker_port()
    if api_port is None:
        print("no running broker found. Start it with `escarp daemon`.", file=sys.stderr)
        return 4

    cfg = load_pool_config()
    cdp_base = cfg.cdp_base
    slot = args.slot

    try:
        resp = httpx.post(
            f"http://127.0.0.1:{api_port}/pool/remove",
            json={"slot": slot, "force": args.force},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        print(f"remove failed: {exc}", file=sys.stderr)
        return 1
    if resp.status_code == 404:
        print(f"slot {slot} is not in the pool.", file=sys.stderr)
        return 1
    if resp.status_code == 409:
        print(
            f"slot {slot} is leased to an agent; re-run with --force to remove it anyway.",
            file=sys.stderr,
        )
        return 3
    if resp.status_code != 200:
        print(f"remove failed: HTTP {resp.status_code}: {resp.text}", file=sys.stderr)
        return 1

    print(f"[slot {slot}] removed from the live pool (no daemon restart).")

    if not args.keep_chrome:
        killed = terminate_chrome_on_port(cdp_base + slot)
        print(
            f"[slot {slot}] chrome on cdp_port {cdp_base + slot}: "
            f"{'terminated' if killed else 'not running'}"
        )
    if not args.keep_data:
        remove_slot_data(slot, cua_apps=cfg.cua_apps)

    _persist_size(cfg, cdp_base=cdp_base, cua_apps=cfg.cua_apps, slots=_current_slots(api_port))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="escarp pool",
        description="add/remove a single slot on a running daemon, no restart.",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    add = sub.add_parser("add", help="add one slot to the running pool")
    add.add_argument("slot", type=int, nargs="?", default=None, help="slot index (default: lowest free)")
    add.add_argument("--cdp-base", type=int, default=None, help="cdp port for slot 0 (default: persisted)")
    add.add_argument(
        "--cua-apps",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="launch the slot from a per-slot app bundle (default: persisted / autodetected)",
    )
    add.add_argument(
        "--no-launch",
        action="store_true",
        help="require a chrome already listening on the slot; do not launch one",
    )

    rem = sub.add_parser("remove", help="remove one slot from the running pool")
    rem.add_argument("slot", type=int, help="slot index to remove")
    rem.add_argument("--force", action="store_true", help="remove even if the slot is leased")
    rem.add_argument("--keep-chrome", action="store_true", help="leave the chrome running")
    rem.add_argument("--keep-data", action="store_true", help="keep the slot's profile and cua bundle")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.action == "add":
        return _add(args)
    return _remove(args)
