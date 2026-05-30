"""`escarp window <slot>` -- the OS-window identity primitive.

The mental model for v1.1:

    Escarp leases OS windows, not just CDP endpoints.
    CDP is the automation transport.
    CUA is the visible interaction transport.
    The OS window identity is the common anchor.

This command surfaces the identity AND actively verifies it. A stale
kCGWindowNumber without verification is just another weak label.
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

from escarp.broker.api import DEFAULT_PORT

BROKER_URL = f"http://127.0.0.1:{DEFAULT_PORT}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="escarp window")
    parser.add_argument("slot", type=int, help="slot index")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit a structured JSON payload (for agents/tests)",
    )
    parser.add_argument(
        "--verify-key",
        action="store_true",
        help="exit nonzero unless the slot's window is key/frontmost",
    )
    return parser


def _human_readable(slot: dict, verify: dict) -> str:
    bounds = slot.get("bounds")
    bounds_str = (
        f"x={int(bounds[0])} y={int(bounds[1])} w={int(bounds[2])} h={int(bounds[3])}"
        if bounds
        else "(no calibrated bounds)"
    )
    lines = [
        f"slot {slot['slot']}",
        f"  os_window_id:      {slot.get('os_window_id') or '(not calibrated)'}",
        f"  owner_pid:         {slot.get('owner_pid') or '(unknown)'}",
        "  app:               Google Chrome for Testing",
        f"  bounds:            {bounds_str}",
        f"  cdp_port:          {slot['cdp_port']}",
        f"  cdp_window_id:     {slot.get('cdp_window_id') or '(unknown)'}",
        f"  cdp_target_id:     {slot.get('cdp_target_id') or '(unknown)'}",
        f"  verified_alive:    {str(verify['verified_alive']).lower()}",
        f"  verified_app:      {str(verify['verified_app']).lower()}",
        f"  verified_bounds:   {str(verify['verified_bounds']).lower()}",
        f"  verified_key:      {str(verify['verified_key']).lower()}",
    ]
    if verify.get("notes"):
        for n in verify["notes"]:
            lines.append(f"  note:              {n}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        resp = httpx.get(f"{BROKER_URL}/status", timeout=3.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(
            f"could not reach broker at {BROKER_URL}: {exc}\n"
            f"start the daemon first: escarp daemon",
            file=sys.stderr,
        )
        return 2

    status = resp.json()
    slot = next((s for s in status["slots"] if s["slot"] == args.slot), None)
    if slot is None:
        print(
            f"slot {args.slot} not in pool (size={status['pool_size']})",
            file=sys.stderr,
        )
        return 2

    os_window_id = slot.get("os_window_id")
    if os_window_id is None:
        verify = {
            "os_window_id": None,
            "verified_alive": False,
            "verified_app": False,
            "verified_bounds": False,
            "verified_key": False,
            "notes": [
                "slot not calibrated -- no OS window identity bound. "
                "Try restarting the daemon on a system with the OS-window "
                "calibration code path (macOS today)."
            ],
        }
    else:
        # Active verification. Import here so non-darwin platforms don't fail
        # on import if pyobjc isn't installed.
        from escarp.broker.macos import verify_window

        bounds_tuple: tuple[float, float, float, float] | None = None
        bounds_in = slot.get("bounds")
        if bounds_in:
            bounds_tuple = (
                float(bounds_in[0]),
                float(bounds_in[1]),
                float(bounds_in[2]),
                float(bounds_in[3]),
            )

        v = verify_window(
            os_window_id,
            expected_bounds=bounds_tuple,
            expected_owner_pid=slot.get("owner_pid"),
        )
        verify = {
            "os_window_id": v.os_window_id,
            "verified_alive": v.verified_alive,
            "verified_app": v.verified_app,
            "verified_bounds": v.verified_bounds,
            "verified_key": v.verified_key,
            "current_owner_pid": v.current_owner_pid,
            "current_bounds": list(v.current_bounds) if v.current_bounds else None,
            "notes": v.notes,
        }

    if args.as_json:
        out = {
            "slot": slot["slot"],
            "os_window_id": slot.get("os_window_id"),
            "owner_pid": slot.get("owner_pid"),
            "bounds": (
                {
                    "x": slot["bounds"][0],
                    "y": slot["bounds"][1],
                    "width": slot["bounds"][2],
                    "height": slot["bounds"][3],
                }
                if slot.get("bounds")
                else None
            ),
            "cdp_port": slot["cdp_port"],
            "cdp_window_id": slot.get("cdp_window_id"),
            "cdp_target_id": slot.get("cdp_target_id"),
            "cdp_ws_url": slot["cdp_ws_url"],
            "verified_alive": verify["verified_alive"],
            "verified_app": verify["verified_app"],
            "verified_bounds": verify["verified_bounds"],
            "verified_key": verify["verified_key"],
            "verification_notes": verify.get("notes") or [],
        }
        print(json.dumps(out, indent=2))
    else:
        print(_human_readable(slot, verify))

    if args.verify_key and not verify["verified_key"]:
        return 1
    return 0
