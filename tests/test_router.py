"""Router tests — v0 stub always returns Mode A."""

from escarp.router import Mode, route


def test_route_returns_mode_a() -> None:
    decision = route("find apartments in West Lafayette")
    assert decision.mode == Mode.AUTONOMOUS


def test_route_reason_is_populated() -> None:
    decision = route("any task")
    assert decision.reason
    assert "v0" in decision.reason


def test_route_is_deterministic() -> None:
    assert route("task A") == route("task A")
