"""Tests for setup_cmd: preflight checks + smoke test + dispatch logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from escarp import setup_cmd


def test_escarp_mcp_path_finds_via_sys_executable(tmp_path: Path) -> None:
    """The setup command must locate escarp-mcp using sys.executable's bin dir
    so it works in venvs that aren't on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_mcp = bin_dir / "escarp-mcp"
    fake_mcp.write_text("#!/bin/sh\n")
    fake_mcp.chmod(0o755)

    with patch("escarp.setup_cmd.sys.executable", str(bin_dir / "python")):
        path = setup_cmd._escarp_mcp_path()

    assert path == fake_mcp


def test_escarp_mcp_path_falls_back_to_which(tmp_path: Path) -> None:
    with patch("escarp.setup_cmd.sys.executable", "/nonexistent/python"):
        with patch("escarp.setup_cmd.shutil.which", return_value="/usr/local/bin/escarp-mcp"):
            path = setup_cmd._escarp_mcp_path()
    assert str(path) == "/usr/local/bin/escarp-mcp"


def test_check_cft_missing() -> None:
    with patch("escarp.setup_cmd.find_cft_binary", return_value=None):
        r = setup_cmd.check_cft()
    assert not r.ok
    assert "npx @puppeteer/browsers" in (r.fix_hint or "")


def test_check_cft_present(tmp_path: Path) -> None:
    fake = tmp_path / "chrome"
    fake.write_text("")
    with patch("escarp.setup_cmd.find_cft_binary", return_value=fake):
        r = setup_cmd.check_cft()
    assert r.ok
    assert str(fake) in r.detail


def test_check_daemon_up() -> None:
    fake_resp = MagicMock()
    fake_resp.raise_for_status = lambda: None
    fake_resp.json = lambda: {"pool_size": 2, "slots": []}
    with patch("escarp.setup_cmd.httpx.get", return_value=fake_resp):
        r = setup_cmd.check_daemon()
    assert r.ok
    assert "pool_size=2" in r.detail


def test_check_daemon_down() -> None:
    with patch("escarp.setup_cmd.httpx.get", side_effect=httpx.ConnectError("nope")):
        r = setup_cmd.check_daemon()
    assert not r.ok
    assert "escarp daemon" in (r.fix_hint or "")


def test_smoke_test_full_path() -> None:
    """Mock the broker's HTTP API to walk acquire -> status (leased) ->
    release -> status (free) and verify the smoke test reports OK."""

    lease = {
        "slot": 0,
        "cdp_port": 9222,
        "cdp_ws_url": "ws://127.0.0.1:9222/devtools/browser/x",
        "lease_token": "fake-token",
    }

    def fake_post(url: str, json: dict | None = None, timeout: float = 5.0) -> MagicMock:
        r = MagicMock()
        r.raise_for_status = lambda: None
        if url.endswith("/acquire"):
            r.json = lambda: lease
        elif url.endswith("/release"):
            r.json = lambda: {**lease, "state": "free"}
        return r

    calls = {"status_get_count": 0}

    def fake_get(url: str, timeout: float = 2.0) -> MagicMock:
        r = MagicMock()
        r.raise_for_status = lambda: None
        if url.endswith("/status"):
            calls["status_get_count"] += 1
            state = "leased" if calls["status_get_count"] == 1 else "free"
            r.json = lambda state=state: {"slots": [{"slot": 0, "state": state}]}
        return r

    with patch("escarp.setup_cmd.httpx.post", side_effect=fake_post):
        with patch("escarp.setup_cmd.httpx.get", side_effect=fake_get):
            ok = setup_cmd._smoke_test()

    assert ok
    assert calls["status_get_count"] == 2


def test_main_dispatch_codex_alias() -> None:
    with patch("escarp.setup_cmd.setup_codex", return_value=0) as fake_setup:
        rc = setup_cmd.main(["codex-cua"])
    assert rc == 0
    fake_setup.assert_called_once_with([])


def test_main_dispatch_claude_alias() -> None:
    with patch("escarp.setup_cmd.setup_claude", return_value=0) as fake_setup:
        rc = setup_cmd.main(["claude"])
    assert rc == 0
    fake_setup.assert_called_once_with([])


def test_main_unknown_agent_exits_nonzero() -> None:
    rc = setup_cmd.main(["aider"])
    assert rc == 2


def test_main_no_arg_prints_usage() -> None:
    rc = setup_cmd.main([])
    assert rc == 2
