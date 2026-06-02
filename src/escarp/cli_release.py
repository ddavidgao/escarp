"""`escarp release` -- token-free release for human ergonomics.

The broker requires lease tokens to release. Humans can't be expected to
remember tokens between commands, so this CLI uses the local lease state
file (~/.escarp/leases.json) as a token resolver.

Forms:

  escarp release --slot N         -- release the lease this machine holds on slot N
  escarp release --holder NAME    -- release all leases held by NAME
  escarp release --mine           -- release every lease this machine recorded
  escarp release --all            -- alias of --mine (more explicit name)
  escarp release --token TOKEN    -- explicit token release (bypasses local state)
"""

from __future__ import annotations

import argparse
import sys

import httpx

from escarp import lease_state
from escarp.slot_ops import broker_url


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="escarp release")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--slot", type=int, help="release the lease on a specific slot")
    group.add_argument("--holder", help="release leases by holder name")
    group.add_argument("--mine", action="store_true", help="release every lease this machine recorded")
    group.add_argument("--all", action="store_true", dest="mine", help="alias of --mine")
    group.add_argument("--token", help="release a specific lease by token (bypasses local state)")
    return parser


def _release_one(token: str) -> tuple[bool, str]:
    try:
        r = httpx.post(
            f"{broker_url()}/release",
            json={"lease_token": token},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        return False, f"http error: {exc}"
    if r.status_code == 404:
        return False, "broker returned 404 (lease unknown or already expired)"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text}"
    try:
        rec = r.json()
        return True, f"slot {rec['slot']} now state={rec['state']!r}"
    except Exception:
        return True, "released (no detail)"


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    leases_to_release: list[tuple[str, int | None, str]] = []  # (token, slot, label)

    if args.token:
        leases_to_release.append((args.token, None, "<explicit token>"))
    elif args.slot is not None:
        local = lease_state.find_by_slot(args.slot)
        if local is None:
            print(
                f"no local record of a lease on slot {args.slot}. "
                f"Either it expired/was released, or another tool holds it.\n"
                f"Use --token if you have the token explicitly, or "
                f"`curl {broker_url()}/status | jq` to see what's live.",
                file=sys.stderr,
            )
            return 2
        leases_to_release.append((local.lease_token, local.slot, f"slot {local.slot} ({local.holder!r})"))
    elif args.holder:
        matches = lease_state.find_by_holder(args.holder)
        if not matches:
            print(f"no local leases recorded with holder={args.holder!r}.", file=sys.stderr)
            return 2
        for lease in matches:
            leases_to_release.append(
                (lease.lease_token, lease.slot, f"slot {lease.slot} ({lease.holder!r})")
            )
    elif args.mine:
        all_local = lease_state.all_leases()
        if not all_local:
            print("no local leases recorded. nothing to release.")
            return 0
        for lease in all_local:
            leases_to_release.append(
                (lease.lease_token, lease.slot, f"slot {lease.slot} ({lease.holder!r})")
            )

    rc = 0
    for token, slot, label in leases_to_release:
        ok, detail = _release_one(token)
        print(f"  {label}: {'released' if ok else 'FAIL'} -- {detail}")
        # Always clear local state for that token. If broker said unknown, the
        # entry was stale; if released cleanly, also clear.
        if slot is not None:
            lease_state.remove_by_slot(slot)
        elif args.token:
            lease_state.remove_by_token(token)
        if not ok:
            rc = 1
    return rc
