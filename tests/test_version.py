"""Public API surface test."""

import escarp


def test_public_api_exports() -> None:
    assert hasattr(escarp, "Agent")
    assert hasattr(escarp, "run")
    assert hasattr(escarp, "RouteDecision")
