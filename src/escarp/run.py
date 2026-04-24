"""Top-level run() convenience function."""

from __future__ import annotations

from escarp.agent import Agent


def run(task: str) -> dict[str, object]:
    """One-shot sync entry point. Internally uses asyncio.run()."""
    return Agent(name="default").run(task)
