"""CLI entry point: escarp --version."""

from __future__ import annotations

import argparse

from escarp import __version__


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="escarp",
        description="Identity-aware runtime for AI agents.",
    )
    parser.add_argument("--version", action="version", version=f"escarp {__version__}")
    parser.parse_args()
