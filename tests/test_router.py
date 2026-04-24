"""Router tests."""

from escarp.router import Mode, route


def test_route_returns_mode_a() -> None:
    decision = route("find apartments in West Lafayette")
    assert decision.mode == Mode.AUTONOMOUS
