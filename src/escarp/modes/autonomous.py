"""Mode A — Autonomous execution.

The agent acts as itself with its own Ed25519 identity and ephemeral workspace.
Outbound requests are signed per RFC9421 / Web Bot Auth.
"""

from __future__ import annotations


async def run_autonomous(task: str) -> dict[str, object]:
    """Execute a task in Mode A. Returns a result dict."""
    raise NotImplementedError(
        "Mode A execution body is built in Day 2+. "
        "Scaffolding is in place; signing and workspace wiring come next."
    )
