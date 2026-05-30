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
    return parser


def _cua_prompt_for(slot: int) -> str:
    title = slot_title(slot)
    return (
        f'Use the currently frontmost Chrome for Testing window (titled "{title}"). '
        f"Do not switch windows, switch apps, or open new browsers -- act only in this "
        f"specific window. [YOUR TASK HERE]"
    )


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
        focus_result = asyncio.run(
            focus_slot(
                slot=record["slot"],
                cdp_port=record["cdp_port"],
                cdp_ws_url=record["cdp_ws_url"],
            )
        )
        print()
        print(f"Focused Chrome for Testing window for slot {record['slot']}.")
        print(f"  window title: {focus_result.title_set!r}")
        if focus_result.notes:
            for note in focus_result.notes:
                print(f"  note: {note}")

    if args.prompt:
        print()
        print("Paste this into Codex CUA (then append your task in the bracketed spot):")
        print()
        print(f'  "{_cua_prompt_for(record["slot"])}"')

    print()
    print("When done:")
    print(f"  escarp release --slot {record['slot']}")
    print("  (or: escarp release --mine)")
    return 0
