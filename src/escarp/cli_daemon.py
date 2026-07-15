"""`escarp daemon stop` -- stop the running broker daemon cleanly.

The daemon never kills healthy chromes, so a plain stop leaves the pool
running (the persistence contract): leases are unbrokered, slot locks release,
chromes stay alive for the next `escarp daemon`. Pass --kill-pool to also
terminate every escarp-owned chrome afterward -- processes only: profiles,
cua app bundles, and lockfiles stay on disk (`escarp scale 0` is the full
teardown that also removes those and persists the new size). --kill-pool
refuses while any slot is leased unless --force is given.
"""

from __future__ import annotations

import argparse
import contextlib
import sys

from escarp.broker.daemon import DAEMON_PIDFILE
from escarp.broker.procs import scan_escarp_chromes, terminate_pids
from escarp.slot_ops import find_daemon_pid, leased_slots, read_daemon_pidfile, stop_daemon


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="escarp daemon stop",
        description="stop the broker daemon (chromes stay alive unless --kill-pool).",
    )
    parser.add_argument(
        "--kill-pool",
        action="store_true",
        help="also terminate every escarp-owned chrome (full teardown)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --kill-pool, kill chromes even if their slot is leased",
    )
    parser.add_argument("--grace", type=float, default=8.0, help="seconds to wait for a clean exit")
    return parser


def stop_main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # The lease check must run while the broker is still up to answer it.
    if args.kill_pool and not args.force:
        leased = leased_slots()
        if leased:
            print(
                f"refusing --kill-pool: slot(s) {leased} are leased to an agent. "
                f"Re-run with --force to override.",
                file=sys.stderr,
            )
            return 3

    rc = 0
    pid = find_daemon_pid()
    if pid is None:
        print("no running daemon found.")
        if read_daemon_pidfile() is not None:
            with contextlib.suppress(OSError):
                DAEMON_PIDFILE.unlink(missing_ok=True)
            print(f"cleared stale pidfile {DAEMON_PIDFILE}.")
    else:
        print(f"stopping daemon pid {pid} ...")
        if stop_daemon(pid, grace=args.grace):
            print("daemon stopped." + ("" if args.kill_pool else " chromes stay alive."))
        else:
            print(f"daemon pid {pid} did not exit within {args.grace}s.", file=sys.stderr)
            rc = 1

    if args.kill_pool:
        pids = [p.pid for p in scan_escarp_chromes()]
        if pids:
            killed = terminate_pids(pids)
            print(f"killed {len(killed)} pool chrome(s): pids {killed}")
        else:
            print("no pool chromes running.")
    return rc
