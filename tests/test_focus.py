"""Tests for the focus primitive (v1.1 CUA integration)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from escarp.broker.focus import FocusResult, focus_slot, slot_title


def test_slot_title_format() -> None:
    assert slot_title(0) == "escarp-slot-0"
    assert slot_title(7) == "escarp-slot-7"


def test_focus_result_succeeded_requires_cdp_at_minimum() -> None:
    r = FocusResult(
        slot=0,
        cdp_port=9222,
        title_set="escarp-slot-0",
        cdp_bring_to_front=False,
        os_app_activated=True,
        os_window_promoted=True,
        notes=[],
    )
    assert not r.succeeded()  # CDP failed -> can't succeed regardless of OS layers


def test_focus_result_succeeded_on_linux_skips_macos_layers() -> None:
    r = FocusResult(
        slot=0,
        cdp_port=9222,
        title_set="escarp-slot-0",
        cdp_bring_to_front=True,
        os_app_activated=False,
        os_window_promoted=False,
        notes=[],
    )
    with patch("escarp.broker.focus.sys.platform", "linux"):
        assert r.succeeded()  # Linux doesn't need macOS-layer success


async def test_focus_slot_returns_failure_if_cdp_layer_dies() -> None:
    """If we can't reach /json/list, succeeded() must be False even on macOS."""
    with patch("escarp.broker.focus.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_cls.return_value = mock_client

        # Mock subprocess so OS layers are silent no-ops.
        with patch("escarp.broker.focus.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=b"", stderr=b"", returncode=0)
            result = await focus_slot(
                slot=0,
                cdp_port=9222,
                cdp_ws_url="ws://127.0.0.1:9222/devtools/browser/x",
            )

    assert not result.cdp_bring_to_front
    assert "CDP layer failed" in " ".join(result.notes)


async def test_focus_slot_skips_macos_layers_on_non_darwin() -> None:
    """Linux/Windows should not even attempt the osascript path."""
    fake_tabs = [
        {"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/abc"},
    ]

    with patch("escarp.broker.focus.sys.platform", "linux"):
        with patch("escarp.broker.focus.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_resp = MagicMock()
            mock_resp.json = MagicMock(return_value=fake_tabs)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_cls.return_value = mock_client

            with patch("escarp.broker.focus._cdp_set_title_and_activate") as cdp_call:
                cdp_call.return_value = None
                with patch("escarp.broker.focus.subprocess.run") as mock_run:
                    result = await focus_slot(
                        slot=2,
                        cdp_port=9224,
                        cdp_ws_url="ws://127.0.0.1:9224/devtools/browser/x",
                    )
                    # `succeeded()` reads sys.platform too -- keep the patch in scope.
                    succeeded_on_linux = result.succeeded()

    assert result.cdp_bring_to_front
    assert not result.os_app_activated
    assert not result.os_window_promoted
    mock_run.assert_not_called()
    assert succeeded_on_linux
