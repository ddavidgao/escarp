# escarp

**Escarp leases OS windows, not just CDP endpoints.** A single broker daemon
owns a pool of persistent Chrome for Testing windows, binds each one to a
stable OS-level window identity (`kCGWindowNumber` on macOS), and hands those
identities — along with their CDP transport URLs — to coding agents over MCP.

> The mental model:
> - **CDP** is the *automation* transport (deterministic, no visible cursor, concurrent-safe).
> - **CUA** is the *visible interaction* transport (real cursor, OS overlays, native dialogs).
> - **The OS window identity is the common anchor.**
> Whether your agent drives via CDP or CUA, both ultimately act on the same persistent OS window. Escarp's job is to make that window addressable and verifiable.

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
escarp launch-pool          # spawn N detached CfT windows (default 4)
escarp daemon &             # discover them, broker leases, run reaper
escarp setup codex          # or: setup claude-code
```

Look at the pool:

```bash
escarp window 0
# slot 0
#   os_window_id:      2290
#   owner_pid:         9267
#   app:               Google Chrome for Testing
#   bounds:            x=40 y=80 w=760 h=580
#   cdp_port:          9222
#   cdp_window_id:     1373420132
#   cdp_target_id:     0B24D11F2E538119159DC810FD2C4914
#   verified_alive:    true
#   verified_app:      true
#   verified_bounds:   true
#   verified_key:      false
#   note:              not key/frontmost; window 2300 (pid 9282, ...) is.
```

Every field after `verified_*` is *queried live* — not from cached state.

Lease + drive a window:

```bash
escarp acquire --holder me --focus --prompt
# ... drive via Codex Desktop / Playwright / chrome-devtools-mcp ...
escarp release --mine
```

## Commands

| Command | Purpose |
|---|---|
| `escarp launch-pool` | Spawn N detached Chrome for Testing windows. One-shot; chromes outlive this command. Idempotent (skips slots already listening). |
| `escarp daemon` | Discover live chromes, **calibrate each to an OS-window identity**, broker leases on `127.0.0.1:7878`, run the reaper. Does NOT own chrome lifecycles. |
| **`escarp window <slot>`** | **Print and *actively verify* a slot's OS-window identity.** The v1.1 primitive. Returns os_window_id, owner_pid, bounds, plus `verified_alive`/`verified_app`/`verified_bounds`/`verified_key` — every check queried against the live OS at the moment of the call. Supports `--json` and `--verify-key` (exit nonzero if not key/frontmost). |
| `escarp acquire --holder X [--focus] [--prompt]` | Lease a slot. Persists token to `~/.escarp/leases.json`. |
| `escarp release {--mine \| --slot N \| --holder NAME \| --token T}` | Token-free release for humans. |
| `escarp focus <slot>` | Best-effort helper that uses the OS-window identity to bring a slot's window forward. CDP `Page.bringToFront` + `osascript activate` + AX raise by geometric match, with post-focus verification against `os_window_id`. Reports success only when the identity check confirms the right window is key. |
| `escarp setup codex` | Idempotent: preflight (CfT, daemon, pool, MCP path, codex CLI), register `escarp-mcp`, smoke test. |
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
  "os_window_id": 2300,
  "owner_pid": 9282,
  "bounds": {"x": 820, "y": 80, "width": 760, "height": 580},
  "cdp_port": 9223,
  "cdp_window_id": 783471201,
  "cdp_target_id": "...",
  "cdp_ws_url": "ws://127.0.0.1:9223/devtools/browser/...",
  "dev_port": null,
  "expires_at": 1780174370,
  "auto_heartbeat_interval_s": 60.0
}
```

`escarp_status` returns the same shape per slot. Auto-heartbeat keeps the
lease alive while the shim is running.

## CDP vs CUA — pick the right transport

Both transports act on the same persistent OS window. Pick by what you need:

| Property | CUA (Codex Desktop) | CDP (Playwright / chrome-devtools-mcp) |
|---|---|---|
| Visible OS cursor | ✅ moves on screen | ❌ no cursor movement |
| Native OS overlays (file pickers, downloads, password manager, permission sheets) | ✅ fully supported | ❌ DOM only |
| Determinism | screenshot + AX tree per turn | deterministic CDP commands |
| Concurrent multi-window | ❌ **one CUA-controlled window per app bundle** — see below | ✅ true parallel agents |
| Window targeting | by app + frontmost (escarp owns making the right window frontmost) | by leased `cdp_ws_url` (window-addressable directly) |
| Best for | end-user-facing tasks, demos, anything with native UI | dev automation, parallel test runs, headless |

**Why "one CUA per app bundle":** Codex CUA's addressing model is per-app — it
operates on the *key window* of an app per turn. Two CUA sessions against two
CfT windows would both resolve to "the CfT app → its frontmost window." The
[`research/cua_targeting.md`](research/cua_targeting.md) report has the
static-analysis evidence.

Escarp's job for native CUA work is therefore: **make the right persistent
window the OS-foreground key window**, then have the agent act on it. The
identity primitive (`escarp window <slot>`) is how you verify the handoff
succeeded — without it, focus is just narrative.

## HTTP API

| Verb | Body | Returns |
| ---- | ---- | ------- |
| `GET  /status` | -- | Pool snapshot incl. os_window_id, owner_pid, bounds, cdp_window_id per slot |
| `POST /acquire` | `{"holder": str, "slot"?: int, "dev_port"?: int}` | Lease record with full identity payload |
| `POST /heartbeat` | `{"lease_token": str}` | Refreshed lease |
| `POST /release` | `{"lease_token": str}` | Lease in `state: free` |
| `GET  /reaped` | -- | Last 50 TTL-expired reclamations (debug) |

## Architecture

**Control plane:** slot allocator with kernel-flock atomicity, OS-window
calibration (Chromium `Browser.setWindowBounds` to a unique per-slot
rectangle, then `CGWindowListCopyWindowInfo` to bind by bounds), lease broker
with TTL + reaper, HTTP API on 7878, MCP shim. **Data plane:** Codex CUA via
OS Accessibility, or Playwright / Chrome DevTools MCP / any CDP client over
the leased `cdp_ws_url`. Escarp provisions and points; it does not proxy
clicks.

The persistence contract is the load-bearing trick: chromes are launched
detached (`start_new_session=True`) and reparent to launchd/init. The daemon
discovers them by `/json/version`; it never owns their lifecycle. Kill the
daemon, chromes stay up. Kill an agent mid-task, the reaper reclaims its
lease within one sweep interval (default 2 s). On every release boundary the
broker creates a fresh `about:blank` tab and closes the rest — no state
inherits across holders.

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

Single-window native-CUA flow:

```bash
escarp acquire --holder cua-demo --focus --prompt
# paste the printed preamble into Codex Desktop, append a task
# verify the bridge:
escarp window <slot> --verify-key       # exits 0 iff the right OS window is key
escarp release --mine
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
            os_window_id = <calibrated at daemon startup>
```

## Status

v1.1.0.

**Claims that hold:**
- Each slot has a stable, actively-verifiable OS-window identity (macOS today). `escarp window <slot>` queries it live.
- Two agents on different slots drive their own leased CfTs over CDP without colliding.
- Killing an agent returns its browser within one reaper interval.
- Pool exhaustion returns a structured 409, not a hang.
- No lockfiles outside the broker's single source of truth.

**Claims that do NOT hold (and aren't claimed):**
- Two concurrent native Codex CUA agents on two CfT windows. Not supported today — CUA addresses by app, not window. Use CDP for concurrent multi-agent work; use CUA for single-window high-fidelity work.

**Not in 1.1:** delegated and supervised identity tiers, cross-machine pooling, Linux/Windows OS-window calibration.

## License

MIT
