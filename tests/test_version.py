"""Version + import smoke tests for the v2 public surface."""

import escarp


def test_version_is_string() -> None:
    assert isinstance(escarp.__version__, str)
    # v2 onward: major version is 1+. Guards against accidental 0.x revert.
    assert int(escarp.__version__.split(".")[0]) >= 1


def test_broker_subpackage_importable() -> None:
    from escarp.broker import claim_slot, ports_for_slot  # noqa: F401
    from escarp.broker.discovery import discover_pool  # noqa: F401
    from escarp.broker.lease import Broker  # noqa: F401


def test_cli_entry_points_importable() -> None:
    from escarp.broker.daemon import main as daemon_main  # noqa: F401
    from escarp.broker.launcher import main as launcher_main  # noqa: F401
    from escarp.cli import main as cli_main  # noqa: F401
    from escarp.mcp.server import main as mcp_main  # noqa: F401
