"""Agent — the primary user-facing object."""

from __future__ import annotations

import asyncio

from escarp.router import Mode, RouteDecision, route


class Agent:
    def __init__(self, name: str, mode: Mode | None = None) -> None:
        self.name = name
        self._mode_override = mode

    def run(self, task: str) -> dict[str, object]:
        """Synchronous facade — safe to call outside an async context."""
        return asyncio.run(self.arun(task))

    async def arun(self, task: str) -> dict[str, object]:
        """Async entry point."""
        decision = self._decide(task)
        return await self._dispatch(task, decision)

    def _decide(self, task: str) -> RouteDecision:
        if self._mode_override is not None:
            return RouteDecision(
                mode=self._mode_override,
                reason=f"explicit override by caller (agent={self.name!r})",
            )
        return route(task)

    async def _dispatch(self, task: str, decision: RouteDecision) -> dict[str, object]:
        from escarp.modes.autonomous import run_autonomous
        from escarp.modes.delegated import run_delegated
        from escarp.modes.supervised import run_supervised

        if decision.mode == Mode.AUTONOMOUS:
            return await run_autonomous(task)
        elif decision.mode == Mode.DELEGATED:
            return await run_delegated(task)
        elif decision.mode == Mode.SUPERVISED:
            return await run_supervised(task)
        else:
            raise ValueError(f"Unknown mode: {decision.mode}")
