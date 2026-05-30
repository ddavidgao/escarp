"""Discovery: probe + discover_pool with mocked HTTP."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from escarp.broker.discovery import discover_pool, probe


@pytest.fixture
def fake_browser_response() -> dict:
    return {
        "Browser": "Chrome/149.0.7827.54",
        "Protocol-Version": "1.3",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/fake-uuid",
    }


async def test_probe_returns_json_when_chrome_responding(fake_browser_response: dict) -> None:
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_response = AsyncMock()
        mock_response.raise_for_status = lambda: None
        mock_response.json = lambda: fake_browser_response
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await probe(9222, timeout=0.5)
        assert result is not None
        assert result["Browser"] == "Chrome/149.0.7827.54"
        assert result["webSocketDebuggerUrl"].startswith("ws://127.0.0.1:9222/")


async def test_probe_returns_none_when_no_listener() -> None:
    # cdp_port that no chrome is bound on. probe() should return None, not raise.
    result = await probe(59999, timeout=0.3)
    assert result is None


async def test_discover_pool_reports_missing_slots() -> None:
    """Walk a 3-slot pool where nothing's listening; all should be missing."""
    discovered, missing = await discover_pool(
        pool_size=3, cdp_base_port=59000, wait_for_each=0
    )
    assert discovered == []
    assert missing == [0, 1, 2]


async def test_discover_pool_with_mocked_listeners(fake_browser_response: dict) -> None:
    """Mock probe() so slots 0 and 2 respond, slot 1 does not."""
    def fake_response_for(port: int) -> dict | None:
        if port in (9222, 9224):
            return {
                **fake_browser_response,
                "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/uuid-{port}",
            }
        return None

    async def fake_probe(cdp_port: int, *, timeout: float = 2.0) -> dict | None:
        return fake_response_for(cdp_port)

    with patch("escarp.broker.discovery.probe", side_effect=fake_probe):
        discovered, missing = await discover_pool(pool_size=3, cdp_base_port=9222)

    assert [b.slot for b in discovered] == [0, 2]
    assert missing == [1]
    assert discovered[0].cdp_port == 9222
    assert discovered[1].cdp_port == 9224
    assert "uuid-9222" in discovered[0].cdp_ws_url
    assert "uuid-9224" in discovered[1].cdp_ws_url


async def test_discover_pool_skips_response_without_ws_url() -> None:
    """A response that's missing webSocketDebuggerUrl is treated as missing."""
    async def fake_probe(cdp_port: int, *, timeout: float = 2.0) -> dict | None:
        return {"Browser": "Chrome/?", "Protocol-Version": "1.3"}  # no ws url

    with patch("escarp.broker.discovery.probe", side_effect=fake_probe):
        discovered, missing = await discover_pool(pool_size=2, cdp_base_port=9222)

    assert discovered == []
    assert missing == [0, 1]
