# escarp

**Escarp leases persistent browser slots, not just CDP endpoints.** A single
broker daemon owns a pool of persistent Chrome for Testing slots, binds each
slot to a stable identity, and hands that identity — along with its CDP
transport URL — to coding agents over MCP.

> The mental model:
> - **CDP** is the *automation* transport (deterministic, no visible cursor, concurrent-safe).
> - **CUA** is the *visible interaction* transport (real cursor, OS overlays, native dialogs).
> - **The slot identity is the common anchor.**
> CDP uses the leased DevTools URL. Native Codex CUA uses the leased per-slot
> app bundle identity. Escarp's job is to make the slot addressable and
> verifiable before an agent touches it.

## Install

```bash
pip install escarp
```

Requirements: Python 3.11+ and a Chrome for Testing binary on disk. On macOS
you also get OS-window identity binding for free (via PyObjC).

```bash
npx @puppeteer/browsers install chrome@stable
```

## Quick start

```bash
escarp launch-pool --cua-apps  # spawn N detached CfT slots with per-slot app IDs
escarp daemon &             # discover them, broker leases, run reaper
escarp setup codex          # or: setup claude-code
```

`escarp setup codex` is a convenience check + MCP registration step. If it
reports a Codex CLI/MCP smoke-test issue but `escarp acquire --prompt --hold`
prints a bundle-ID prompt and the browser slots are running, the native CUA
flow can still work. Treat setup failures as "MCP wiring needs attention," not
as proof that the browser pool is unusable.

### Codex CUA quickstart

For visible browser tasks in Codex, use Escarp's per-slot app identities and
drive the leased slot with Codex Computer Use (CUA).

```bash
pip install escarp
npx @puppeteer/browsers install chrome@stable

escarp launch-pool --cua-apps
escarp daemon
escarp setup codex
escarp docs codex-cua
```

For a one-off native-CUA session:

```bash
escarp acquire --holder codex-cua --focus --prompt --hold
```

Paste the printed prompt into Codex, then append your browser task.

Important: Escarp slot acquisition alone is not the complete native-CUA
workflow. After acquiring a slot, Codex must target the leased per-slot app
identity, such as `Escarp Chrome Slot 0`, through Computer Use. CDP,
Playwright, curl, and DevTools are diagnostics or automation transports, not
the primary visible browser control path.

Look at the pool:

```bash
escarp window 0
# slot 0
#   os_window_id:      (not calibrated)
#   owner_pid:         (unknown)
#   app:               Google Chrome for Testing
#   cua_app:           dev.escarp.chrome.slot0
#   bounds:            (unknown)
#   cdp_port:          9222
#   cdp_window_id:     (unknown)
#   cdp_target_id:     (unknown)
#   verified_alive:    false
#   note:              CUA app identity mode; OS-window calibration skipped
```

Every field after `verified_*` is *queried live* — not from cached state.

Lease + drive a window:

```bash
escarp acquire --holder me --prompt --hold
# ... drive via Codex Desktop / Playwright / chrome-devtools-mcp ...
# press Ctrl-C in the acquire terminal when done; it releases and resets the slot
```

## Commands

| Command | Purpose |
|---|---|
| `escarp launch-pool [--cua-apps]` | Spawn N detached Chrome for Testing slots. One-shot; chromes outlive this command. Idempotent (skips slots already listening). `--cua-apps` creates per-slot macOS app bundle identities so native Codex CUA can target slots as separate apps. |
| `escarp daemon` | Discover live chromes, broker leases on `127.0.0.1:7878`, run the reaper. In CUA app mode it records the per-slot bundle identity and avoids resizing windows; otherwise it calibrates each slot to an OS-window identity. Does NOT own chrome lifecycles. |
| **`escarp window <slot>`** | **Print a slot's identity.** In CUA app mode this is the per-slot app bundle ID. In OS-window mode it also returns `os_window_id`, owner_pid, bounds, and live verification fields. Supports `--json` and `--verify-key` for OS-window mode. |
| `escarp acquire --holder X [--focus] [--prompt] [--hold]` | Lease a slot. Persists token to `~/.escarp/leases.json`. `--hold` heartbeats in the foreground until Ctrl-C, then releases the lease. |
| `escarp release {--mine \| --slot N \| --holder NAME \| --token T}` | Token-free release for humans. |
| `escarp docs [codex-cua]` | List bundled docs with their installed path, or print the Codex CUA quickstart so it can be pasted into Codex/runbooks. |
| `escarp focus <slot>` | Best-effort helper that uses the OS-window identity to bring a slot's window forward. CDP `Page.bringToFront` + `osascript activate` + AX raise by geometric match, with post-focus verification against `os_window_id`. Reports success only when the identity check confirms the right window is key. |
| `escarp setup codex` | Convenience preflight + MCP registration. Useful but not required for the CLI native-CUA flow if `escarp acquire --prompt --hold` works. |
| `escarp setup claude-code` | Same shape, for Claude Code. |

## The MCP shim

Register the bundled MCP server:

```bash
escarp setup codex            # or: escarp setup claude-code
```

The model gets three tools that return **structured identity**, not text:

```json
// escarp_acquire returns:
{
  "slot": 1,
  "os_window_id": null,
  "owner_pid": null,
  "bounds": null,
  "cua_app_bundle_id": "dev.escarp.chrome.slot1",
  "cua_app_name": "Escarp Chrome Slot 1",
  "cua_app_path": "/Users/me/.escarp/cua-apps/Escarp Chrome Slot 1.app",
  "cdp_port": 9223,
  "cdp_window_id": null,
  "cdp_target_id": null,
  "cdp_ws_url": "ws://127.0.0.1:9223/devtools/browser/...",
  "dev_port": null,
  "expires_at": 1780174370,
  "auto_heartbeat_interval_s": 60.0
}
```

`escarp_status` returns the same shape per slot. Auto-heartbeat keeps the
lease alive while the shim is running.

When the pool is full, agents do not need to infer whether another holder is
dead. `/status` includes `last_heartbeat`, `expires_at`, `suspected_stale`,
`available_after_s`, and `retry_after_s` per slot. The broker reaper remains
the only authority that frees expired leases; agents should wait for
`retry_after_s`, retry acquire, or surface pool exhaustion.

## CDP vs CUA — pick the right transport

Both transports act on the same persistent slot. Pick by what you need:

| Property | CUA (Codex Desktop) | CDP (Playwright / chrome-devtools-mcp) |
|---|---|---|
| Visible OS cursor | ✅ moves on screen | ❌ no cursor movement |
| Native OS overlays (file pickers, downloads, password manager, permission sheets) | ✅ fully supported | ❌ DOM only |
| Determinism | screenshot + AX tree per turn | deterministic CDP commands |
| Concurrent multi-slot | ✅ on macOS with `escarp launch-pool --cua-apps` | ✅ true parallel agents |
| Slot targeting | by leased per-slot app bundle ID | by leased `cdp_ws_url` |
| Best for | end-user-facing tasks, demos, anything with native UI | dev automation, parallel test runs, headless |

**Why per-slot app bundles:** Codex CUA's addressing model is per-app — it
operates on the *key window* of an app per turn. Two CUA sessions against two
windows from the same CfT bundle both resolve to "the CfT app → its frontmost
window." `escarp launch-pool --cua-apps` fixes that by cloning lightweight
per-slot app bundles such as `dev.escarp.chrome.slot0` and
`dev.escarp.chrome.slot1`, each with its own profile and CDP port. On APFS the
bundle clone is copy-on-write, so disk overhead is mostly metadata until files
diverge.

Escarp's job for native CUA work is therefore: **lease the slot, expose its
bundle ID, and keep the lease alive while the agent is using it.** The prompt
from `escarp acquire --prompt --hold` tells Codex CUA to target that bundle ID
directly and keeps the lease alive while the terminal command is running.

For Claude Code today, Escarp validates cleanly through MCP/CDP
(`escarp setup claude-code` and the leased `cdp_ws_url`). If a Claude Code
build exposes native app-targeted Computer Use, it should use the same per-slot
bundle IDs. Without that native CUA tool, Claude Code does not show the Codex
CUA cursor path.

## HTTP API

| Verb | Body | Returns |
| ---- | ---- | ------- |
| `GET  /status` | -- | Pool snapshot incl. slot identity, holder, heartbeat/expiry, `suspected_stale`, and bounded retry fields |
| `POST /acquire` | `{"holder": str, "slot"?: int, "dev_port"?: int}` | Lease record with full identity payload |
| `POST /heartbeat` | `{"lease_token": str}` | Refreshed lease |
| `POST /release` | `{"lease_token": str}` | Lease in `state: free` |
| `GET  /reaped` | -- | Last 50 TTL-expired reclamations (debug) |

## Architecture

**Control plane:** slot allocator with kernel-flock atomicity, lease broker
with TTL + reaper, HTTP API on 7878, MCP shim, and two identity modes:
per-slot app bundle IDs for native CUA, or OS-window calibration
(`kCGWindowNumber` on macOS) for same-bundle/CDP workflows. **Data plane:**
Codex CUA via OS Accessibility, or Playwright / Chrome DevTools MCP / any CDP
client over the leased `cdp_ws_url`. Escarp provisions and points; it does not
proxy clicks.

The persistence contract is the load-bearing trick: chromes are launched
detached (`start_new_session=True`) and reparent to launchd/init. The daemon
discovers them by `/json/version`; it never owns their lifecycle. Kill the
daemon, chromes stay up. Kill an agent mid-task, the reaper reclaims its
lease within one sweep interval (default 2 s). On every release boundary the
broker creates a fresh `about:blank` tab and closes the rest — no state
inherits across holders.

Lease liveness is broker-owned. Heartbeat is `POST /heartbeat` with the secret
lease token; a valid heartbeat refreshes `last_heartbeat` and extends
`expires_at`. MCP shims send it automatically at `TTL / 3`; CLI native-CUA
sessions should use `escarp acquire --prompt --hold`, which does the same in
the foreground and releases on Ctrl-C. If a holder stops heartbeating, the slot
is not stolen by another agent; it becomes reclaimable only when the broker
reaper observes that `expires_at` has passed.

See [V2_PLAN.md](V2_PLAN.md) for the v0→v2 design notes and
[`research/cua_targeting.md`](research/cua_targeting.md) for the CUA
addressing analysis.

## Demos

```bash
# Two holders, two browsers, asyncio.gather lockstep concurrency (CDP).
# Proves the lease/concurrency model on the CDP transport.
uv run python scripts/demo_two_holders_concurrent.py

# Lease-boundary reset: drive to YouTube, release, watch the tab snap back.
uv run python scripts/demo_reset_on_release.py
```

Native-CUA flow:

```bash
escarp launch-pool --pool-size 2 --cua-apps
ESCARP_POOL_SIZE=2 escarp daemon
escarp acquire --slot 0 --holder cua-demo --prompt --hold
# paste the printed bundle-ID preamble into Codex Desktop, append a task
# press Ctrl-C here when done; escarp releases and resets the slot
```

## Configuration

| Env var | Default | What |
| ------- | ------- | ---- |
| `ESCARP_POOL_SIZE` | `4` | Number of browser slots |
| `ESCARP_CDP_BASE` | `9222` | cdp port for slot 0; slot N uses base+N |
| `ESCARP_API_PORT` | `7878` | Broker HTTP API port (bind-and-shift on collision) |
| `ESCARP_LEASE_TTL_S` | `60` | Lease expiry; reaper reclaims past this |
| `ESCARP_CFT_BINARY` | autodetect | Path to Chrome for Testing binary |
| `ESCARP_BROKER_URL` | `http://127.0.0.1:7878` | Where the MCP shim looks for the broker |
| `ESCARP_LEASES_FILE` | `~/.escarp/leases.json` | Local cache of lease tokens for `escarp release --mine` |

## Per-slot resource derivation

```
slot s  ->  frontend  = 3000 + s*10
            backend   = 8000 + s*10
            postgres  = 5432 + s*10
            cdp_port  = 9222 + s
            user_data = ~/.escarp/profiles/<tier>/slot-<s>
            cua_app   = dev.escarp.chrome.slot<s> when launched with --cua-apps
            os_window_id = <calibrated at daemon startup outside CUA app mode>
```

## Status

v1.3.0.

**Claims that hold:**
- Each native-CUA slot can have a stable per-slot app bundle identity on macOS (`dev.escarp.chrome.slotN`).
- Same-bundle slots still have an actively-verifiable OS-window identity (macOS today). `escarp window <slot>` queries it live.
- Two agents on different slots drive their own leased CfTs over CDP without colliding.
- Two native Codex CUA agents can drive different slots concurrently when the pool is launched with `--cua-apps`.
- Killing an agent returns its browser within one reaper interval.
- Pool exhaustion returns a structured 409, not a hang.
- No lockfiles outside the broker's single source of truth.

**Claims that do NOT hold (and aren't claimed):**
- Two concurrent native CUA agents on two windows from the same app bundle. CUA addresses by app, not by window; use `--cua-apps` for native CUA concurrency.

**Not in 1.1:** delegated and supervised identity tiers, cross-machine pooling, Linux/Windows OS-window calibration.

## License

MIT
