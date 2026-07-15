"""`escarp daemon stop` -- stop the running broker daemon cleanly.

The daemon never kills healthy chromes, so a plain stop leaves the pool
running (the persistence contract): leases are unbrokered, slot locks release,
chromes stay alive for the next `escarp daemon`. Pass --kill-pool to also
terminate every escarp-owned chrome afterward -- the same full teardown
`escarp scale 0` performs, minus persisting a new size.
"""

from __future__ import annotations

import argparse
import contextlib
import sys

from escarp.broker.daemon import DAEMON_PIDFILE
from escarp.broker.procs import scan_escarp_chromes, terminate_pids
from escarp.slot_ops import find_daemon_pid, read_daemon_pidfile, stop_daemon


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
    parser.add_argument("--grace", type=float, default=8.0, help="seconds to wait for a clean exit")
    return parser


def stop_main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

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
