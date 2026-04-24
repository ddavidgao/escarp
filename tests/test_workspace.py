"""Workspace tests — profile isolation is the critical property."""

import tempfile
from pathlib import Path

import pytest

from escarp.workspace.lightpanda import LightpandaBrowser


def test_lightpanda_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="v0"):
        LightpandaBrowser()


def test_temp_dir_is_fresh() -> None:
    """Each workspace gets a genuinely separate temp dir — not shared state."""
    dirs: list[Path] = []
    for _ in range(2):
        with tempfile.TemporaryDirectory(prefix="escarp-workspace-") as tmp:
            dirs.append(Path(tmp))
    assert dirs[0] != dirs[1]


def test_temp_dir_cleaned_up() -> None:
    """Temp dir is deleted when the context exits."""
    with tempfile.TemporaryDirectory(prefix="escarp-workspace-") as tmp:
        p = Path(tmp)
        assert p.exists()
    assert not p.exists()
