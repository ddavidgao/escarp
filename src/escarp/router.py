"""Task router — maps a task description to a RouteDecision.

v0: always returns Mode A. The dataclass and interface are final; v0.3
replaces the stub body with a real Claude-powered classifier.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Mode(Enum):
    AUTONOMOUS = "autonomous"
    DELEGATED = "delegated"
    SUPERVISED = "supervised"


@dataclass(frozen=True)
class RouteDecision:
    mode: Mode
    reason: str


def route(task: str) -> RouteDecision:  # noqa: ARG001
    return RouteDecision(
        mode=Mode.AUTONOMOUS,
        reason="v0: router always selects Mode A (Modes B/C not yet implemented)",
    )
