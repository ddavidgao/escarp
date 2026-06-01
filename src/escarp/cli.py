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
    sub.add_parser(
        "window",
        help="print and actively verify a slot's OS-window identity (the v1.1 primitive).",
        add_help=False,
    )
    focus_parser = sub.add_parser(
        "focus",
        help="best-effort helper that uses the OS-window identity to bring a slot forward.",
    )
    focus_parser.add_argument("slot", type=int, help="slot index to focus")
    setup_parser = sub.add_parser(
        "setup",
        help="one-command MCP wiring + smoke test for an agent (codex, claude-code).",
    )
    setup_parser.add_argument("agent", help="codex (alias: codex-cua) or claude-code (alias: claude)")
    sub.add_parser(
        "acquire",
        help="lease a slot, optionally --prompt a CUA preamble and --hold heartbeats.",
        add_help=False,
    )
    sub.add_parser(
        "release",
        help="release leases held by this machine (--mine / --slot / --holder / --token).",
        add_help=False,
    )
    sub.add_parser(
        "docs",
        help="list or print bundled docs installed with the package.",
        add_help=False,
    )

    args, rest = parser.parse_known_args(argv)

    if args.command == "launch-pool":
        from escarp.broker.launcher import main as launch_main
        return launch_main(rest)
    if args.command == "daemon":
        from escarp.broker.daemon import main as daemon_main
        return daemon_main(rest)
    if args.command == "window":
        from escarp.cli_window import main as window_main
        return window_main(rest)
    if args.command == "focus":
        from escarp.cli_focus import main as focus_main
        return focus_main(args.slot)
    if args.command == "setup":
        from escarp.setup_cmd import main as setup_main
        return setup_main([args.agent, *rest])
    if args.command == "acquire":
        from escarp.cli_acquire import main as acquire_main
        return acquire_main(rest)
    if args.command == "release":
        from escarp.cli_release import main as release_main
        return release_main(rest)
    if args.command == "docs":
        from escarp.cli_docs import main as docs_main
        return docs_main(rest)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
