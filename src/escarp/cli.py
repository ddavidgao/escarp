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
    sub.add_parser("daemon", help="run the broker daemon (pool of persistent CfT windows)")

    args = parser.parse_args(argv)

    if args.command == "daemon":
        from escarp.broker.daemon import main as daemon_main
        return daemon_main(argv)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
