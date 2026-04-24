"""Workspace tests."""

import pytest

from escarp.workspace.lightpanda import LightpandaBrowser


def test_lightpanda_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="v0"):
        LightpandaBrowser()
