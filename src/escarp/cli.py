"""CLI entry point."""

from __future__ import annotations

import argparse
import sys

from escarp import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="escarp",
        description="Identity-aware runtime for parallel coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"escarp {__version__}")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser(
        "launch-pool",
        help="spawn N detached Chrome for Testing processes (one-shot, exits).",
    )
    sub.add_parser(
        "daemon",
        help="discover already-running chromes and broker leases against them.",
    )

    args, rest = parser.parse_known_args(argv)

    if args.command == "launch-pool":
        from escarp.broker.launcher import main as launch_main
        return launch_main(rest)
    if args.command == "daemon":
        from escarp.broker.daemon import main as daemon_main
        return daemon_main(rest)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
