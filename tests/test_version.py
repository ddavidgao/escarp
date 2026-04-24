"""Package version and public API surface tests."""

import escarp


def test_version_is_string() -> None:
    assert isinstance(escarp.__version__, str)


def test_version_format() -> None:
    parts = escarp.__version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_public_api_exports() -> None:
    assert hasattr(escarp, "Agent")
    assert hasattr(escarp, "run")
    assert hasattr(escarp, "RouteDecision")
