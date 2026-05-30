"""`escarp acquire` -- one-stop lease + focus + CUA prompt generator.

The flow David asked for (single-window native CUA validation):

    escarp acquire --holder codex-cua-demo --focus --prompt

  Acquired slot 1.
  Focused Chrome for Testing window for slot 1.

  Paste this into Codex CUA:
    "Use the currently frontmost Chrome for Testing window (titled
    'escarp-slot-1'). Do not switch windows or apps. [YOUR TASK]"

  When done:
    escarp release --slot 1
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

import httpx

from escarp import lease_state
from escarp.broker.api import DEFAULT_PORT
from escarp.broker.focus import focus_slot, slot_title

BROKER_URL_DEFAULT = f"http://127.0.0.1:{DEFAULT_PORT}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="escarp acquire")
    parser.add_argument("--holder", required=True, help="agent identifier (shows up in /status)")
    parser.add_argument("--slot", type=int, help="specific slot to acquire (omit for lowest-free)")
    parser.add_argument("--dev-port", type=int, help="local dev server port the agent will hit")
    parser.add_argument(
        "--focus",
        action="store_true",
        help="bring the leased CfT window to the OS foreground for native CUA driving",
    )
    parser.add_argument(
        "--prompt",
        action="store_true",
        help="emit a paste-ready Codex CUA prompt preamble that targets the focused window",
    )
    parser.add_argument(
        "--hold",
        action="store_true",
        help=(
            "keep heartbeating this lease in the foreground until Ctrl-C, then release it; "
            "recommended with --prompt for native CUA sessions"
        ),
    )
    return parser


def _cua_prompt_for(record: dict) -> str:
    slot = record["slot"]
    bundle_id = record.get("cua_app_bundle_id")
    app_name = record.get("cua_app_name")
    app_path = record.get("cua_app_path")
    if bundle_id:
        return (
            f'Use native Computer Use on the app with bundle identifier "{bundle_id}" '
            f'({app_name or f"Escarp Chrome Slot {slot}"}). Do not switch apps or '
            f"open a different browser. This app is the leased escarp slot {slot}. "
            f"[YOUR TASK HERE]"
        )
    if app_path:
        return (
            f'Use native Computer Use on the app at "{app_path}". Do not switch apps '
            f"or open a different browser. This app is the leased escarp slot {slot}. "
            f"[YOUR TASK HERE]"
        )
    title = slot_title(slot)
    return (
        f'Use the currently frontmost Chrome for Testing window (titled "{title}"). '
        f"Do not switch windows, switch apps, or open new browsers -- act only in this "
        f"specific window. [YOUR TASK HERE]"
    )


def _slot_record(slot: int) -> dict | None:
    resp = httpx.get(f"{BROKER_URL_DEFAULT}/status", timeout=3.0)
    resp.raise_for_status()
    data = resp.json()
    return next((s for s in data["slots"] if s["slot"] == slot), None)


def _release_acquired(record: dict) -> None:
    try:
        httpx.post(
            f"{BROKER_URL_DEFAULT}/release",
            json={"lease_token": record["lease_token"]},
            timeout=10.0,
        )
    finally:
        lease_state.remove_by_slot(record["slot"])


def _lease_ttl_s(default: float = 60.0) -> float:
    try:
        resp = httpx.get(f"{BROKER_URL_DEFAULT}/status", timeout=3.0)
        resp.raise_for_status()
        return float(resp.json().get("lease_ttl_s", default))
    except Exception:
        return default


def _heartbeat(record: dict) -> dict:
    resp = httpx.post(
        f"{BROKER_URL_DEFAULT}/heartbeat",
        json={"lease_token": record["lease_token"]},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


def _hold_until_interrupted(record: dict) -> int:
    interval_s = max(1.0, _lease_ttl_s() / 3)
    print()
    print(
        f"Holding lease for slot {record['slot']}; heartbeating every {interval_s:.0f}s. "
        "Press Ctrl-C when the CUA task is done to release and reset the slot."
    )
    try:
        while True:
            time.sleep(interval_s)
            refreshed = _heartbeat(record)
            record["expires_at"] = refreshed.get("expires_at", record.get("expires_at"))
            print(f"  heartbeat ok; expires_at={record['expires_at']:.0f}", flush=True)
    except KeyboardInterrupt:
        print()
        print(f"Releasing slot {record['slot']}...")
        _release_acquired(record)
        print("Released.")
        return 130
    except httpx.HTTPError as exc:
        print()
        print(
            f"heartbeat failed; this process no longer has a valid live lease: {exc}",
            file=sys.stderr,
        )
        lease_state.remove_by_slot(record["slot"])
        return 5


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    payload: dict[str, object] = {"holder": args.holder}
    if args.slot is not None:
        payload["slot"] = args.slot
    if args.dev_port is not None:
        payload["dev_port"] = args.dev_port

    try:
        resp = httpx.post(f"{BROKER_URL_DEFAULT}/acquire", json=payload, timeout=10.0)
    except httpx.HTTPError as exc:
        print(
            f"could not reach broker at {BROKER_URL_DEFAULT}: {exc}\n"
            f"start the daemon first: escarp daemon",
            file=sys.stderr,
        )
        return 2

    if resp.status_code != 200:
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}
        print(f"acquire failed (HTTP {resp.status_code}): {body}", file=sys.stderr)
        return 3

    record = resp.json()
    local = lease_state.make(record, holder=args.holder)
    lease_state.add(local)

    print(f"Acquired slot {record['slot']} for holder={args.holder!r}.")
    print(f"  cdp_ws_url: {record['cdp_ws_url']}")
    print(f"  cdp_port:   {record['cdp_port']}")
    if record.get("dev_port"):
        print(f"  dev_port:   {record['dev_port']}")
    print(f"  expires_at: {record['expires_at']:.0f}")

    if args.focus:
        try:
            target = _slot_record(record["slot"])
        except Exception as exc:
            print(
                f"could not verify slot {record['slot']} OS-window identity after acquire: {exc}",
                file=sys.stderr,
            )
            _release_acquired(record)
            return 4

        if target is None:
            print(f"slot {record['slot']} disappeared from broker status", file=sys.stderr)
            _release_acquired(record)
            return 4

        bounds = target.get("bounds")
        bounds_tuple = tuple(bounds) if bounds is not None else None
        focus_result = asyncio.run(
            focus_slot(
                slot=record["slot"],
                cdp_port=target["cdp_port"],
                cdp_ws_url=target["cdp_ws_url"],
                cg_window_number=target.get("os_window_id"),
                cg_window_owner_pid=target.get("owner_pid"),
                cg_window_bounds=bounds_tuple,  # type: ignore[arg-type]
            )
        )
        print()
        print(f"Focused Chrome for Testing window for slot {record['slot']}.")
        print(f"  window title: {focus_result.title_set!r}")
        if focus_result.notes:
            for note in focus_result.notes:
                print(f"  note: {note}")
        if not focus_result.succeeded():
            print()
            print(
                "focus verification failed; not emitting a Codex CUA prompt and "
                "releasing the lease to avoid a stale lock.",
                file=sys.stderr,
            )
            _release_acquired(record)
            return 4

    if args.prompt:
        print()
        print("Paste this into Codex CUA (then append your task in the bracketed spot):")
        print()
        print(f'  "{_cua_prompt_for(record)}"')

    if args.hold:
        return _hold_until_interrupted(record)

    print()
    print("When done:")
    print(f"  escarp release --slot {record['slot']}")
    print("  (or: escarp release --mine)")
    return 0
