"""Tests for `escarp acquire` fail-closed CUA prompt behavior."""

from __future__ import annotations

from unittest.mock import Mock

from escarp import cli_acquire
from escarp.broker.focus import FocusResult


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.text)


def _lease_record() -> dict:
    return {
        "slot": 1,
        "holder": "codex-cua-demo",
        "lease_token": "tok-1",
        "cdp_port": 9223,
        "cdp_ws_url": "ws://127.0.0.1:9223/devtools/browser/x",
        "expires_at": 1234567890.0,
        "acquired_at": 1234567800.0,
    }


def _status_record() -> dict:
    return {
        "pool_size": 2,
        "slots": [
            {
                "slot": 1,
                "cdp_port": 9223,
                "cdp_ws_url": "ws://127.0.0.1:9223/devtools/browser/x",
                "os_window_id": 2300,
                "owner_pid": 9282,
                "bounds": [820.0, 80.0, 760.0, 580.0],
            }
        ],
    }


def test_acquire_focus_prompt_passes_os_identity_to_focus(monkeypatch, tmp_path, capsys) -> None:
    post = Mock(return_value=FakeResponse(_lease_record()))
    get = Mock(return_value=FakeResponse(_status_record()))
    captured: dict = {}

    async def fake_focus_slot(**kwargs):
        captured.update(kwargs)
        return FocusResult(
            slot=1,
            cdp_port=9223,
            title_set="escarp-slot-1",
            cdp_bring_to_front=True,
            verified_frontmost=True,
            cg_window_number=2300,
        )

    monkeypatch.setattr(cli_acquire.httpx, "post", post)
    monkeypatch.setattr(cli_acquire.httpx, "get", get)
    monkeypatch.setattr(cli_acquire, "focus_slot", fake_focus_slot)
    monkeypatch.setattr(cli_acquire.lease_state, "add", lambda lease: None)

    rc = cli_acquire.main(["--holder", "codex-cua-demo", "--slot", "1", "--focus", "--prompt"])

    assert rc == 0
    assert captured["cg_window_number"] == 2300
    assert captured["cg_window_owner_pid"] == 9282
    assert captured["cg_window_bounds"] == (820.0, 80.0, 760.0, 580.0)
    assert "Paste this into Codex CUA" in capsys.readouterr().out


def test_acquire_focus_prompt_releases_and_suppresses_prompt_on_focus_failure(
    monkeypatch, capsys
) -> None:
    post = Mock(side_effect=[FakeResponse(_lease_record()), FakeResponse({"state": "free"})])
    get = Mock(return_value=FakeResponse(_status_record()))
    removed_slots: list[int] = []

    async def fake_focus_slot(**kwargs):
        return FocusResult(
            slot=1,
            cdp_port=9223,
            title_set="escarp-slot-1",
            cdp_bring_to_front=True,
            verified_frontmost=False,
            cg_window_number=2300,
            actually_frontmost_cg_window=2290,
            notes=["post-focus check FAILED"],
        )

    monkeypatch.setattr(cli_acquire.httpx, "post", post)
    monkeypatch.setattr(cli_acquire.httpx, "get", get)
    monkeypatch.setattr(cli_acquire, "focus_slot", fake_focus_slot)
    monkeypatch.setattr(cli_acquire.lease_state, "add", lambda lease: None)
    monkeypatch.setattr(cli_acquire.lease_state, "remove_by_slot", lambda slot: removed_slots.append(slot))

    rc = cli_acquire.main(["--holder", "codex-cua-demo", "--slot", "1", "--focus", "--prompt"])
    output = capsys.readouterr()

    assert rc == 4
    assert "Paste this into Codex CUA" not in output.out
    assert "focus verification failed" in output.err
    assert removed_slots == [1]
    assert post.call_args_list[-1].kwargs["json"] == {"lease_token": "tok-1"}


def test_cua_prompt_prefers_per_slot_bundle_id() -> None:
    record = _lease_record() | {
        "cua_app_bundle_id": "dev.escarp.chrome.slot1",
        "cua_app_name": "Escarp Chrome Slot 1",
        "cua_app_path": "/Users/davidgao/.escarp/cua-apps/Escarp Chrome Slot 1.app",
    }

    prompt = cli_acquire._cua_prompt_for(record)

    assert 'bundle identifier "dev.escarp.chrome.slot1"' in prompt
    assert "Escarp Chrome Slot 1" in prompt
    assert "currently frontmost Chrome for Testing window" not in prompt
