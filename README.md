# escarp

**Escarp leases persistent browser slots, not just CDP endpoints.** A single
broker daemon owns a pool of persistent Chrome for Testing slots, binds each
slot to a stable identity, and hands that identity — together with its CDP
transport URL — to coding agents over MCP or the CLI.

## The mental model

A *slot* is a persistent browser plus a stable identity. Two transports can act
on the same slot; the slot identity is the common anchor that both target:

- **CDP** is the *automation* transport: deterministic commands, no visible
  cursor, safe to run many in parallel. Agents target it by the leased
  `cdp_ws_url`.
- **CUA** is the *visible interaction* transport: a real OS cursor, native
  dialogs (file pickers, downloads, password manager). Native Codex CUA targets
  it by the leased per-slot app bundle identity.

Escarp's job is to make a slot addressable and verifiable *before* an agent
touches it, then keep its lease alive while the agent works.

## Install

```bash
pip install escarp
npx @puppeteer/browsers install chrome@stable
```

Requirements: Python 3.11+ and a Chrome for Testing binary on disk. On macOS you
also get OS-window identity binding for free (via PyObjC).

## Quick start

```bash
escarp launch-pool          # spawn N detached Chrome for Testing slots (one-shot)
escarp daemon &             # discover them, broker leases, run the reaper
escarp setup codex          # register the MCP server (or: setup claude-code)
```

Lease a slot and drive it:

```bash
escarp acquire --holder me --prompt --hold
# ... drive the slot via Codex / Playwright / chrome-devtools-mcp ...
# press Ctrl-C to release and reset the slot
```

`--hold` heartbeats the lease in the foreground until Ctrl-C, then releases it.
Without `--hold`, release explicitly with `escarp release --mine`.

Inspect a slot's identity (every field after `verified_*` is queried live, not
cached):

```bash
escarp window 0
# slot 0
#   os_window_id:      (not calibrated)
#   owner_pid:         (unknown)
#   app:               Google Chrome for Testing
#   cua_app:           dev.escarp.chrome.slot0
#   cdp_port:          9222
#   verified_alive:    false
#   note:              CUA app identity mode; OS-window calibration skipped
```

## Native CUA with Codex

For visible browser tasks, launch the pool with per-slot app identities and
drive the leased slot with Codex Computer Use:

```bash
escarp launch-pool --cua-apps   # one lightweight app bundle per slot
escarp daemon
escarp setup codex
escarp acquire --holder codex-cua --focus --prompt --hold
```

`--prompt` prints a paste-ready preamble that tells Codex CUA which per-slot app
bundle (e.g. `Escarp Chrome Slot 0`) to target. Paste it into Codex, append your
task, and press Ctrl-C here when done. `escarp docs codex-cua` prints the full
quickstart.

Acquiring a slot is not the whole CUA workflow: Codex must target the leased
per-slot app identity through Computer Use. CDP, Playwright, and DevTools are
diagnostics or automation transports here, not the visible control path.

**Why per-slot app bundles:** Codex CUA addresses by *app* — it acts on the key
window of an app per turn. Two CUA sessions against two windows of the same CfT
bundle both resolve to "the CfT app → its frontmost window," so they collide.
`--cua-apps` clones a lightweight per-slot bundle (`dev.escarp.chrome.slot0`,
`dev.escarp.chrome.slot1`, …), each with its own profile and CDP port, so each
slot is a distinct app. On APFS the clone is copy-on-write, so disk overhead is
mostly metadata until the files diverge.

For Claude Code today, Escarp validates through MCP/CDP and the leased
`cdp_ws_url`; if a future build exposes native app-targeted Computer Use it
should use the same per-slot bundle IDs.

## Commands

| Command | Purpose |
|---|---|
| `escarp launch-pool [--cua-apps]` | Spawn N detached Chrome for Testing slots. One-shot; chromes outlive the command. Idempotent (skips slots already listening). `--cua-apps` gives each slot its own macOS app bundle so native Codex CUA can target slots separately. Records the size in `~/.escarp/pool.json`. |
| `escarp daemon` | Discover live chromes, broker leases on `127.0.0.1:7878`, run the reaper. Pool size: `ESCARP_POOL_SIZE` env, else `~/.escarp/pool.json`, else 4. Does NOT own chrome lifecycles. |
| `escarp acquire --holder X [--slot N] [--focus] [--prompt] [--hold]` | Lease a slot (lowest-free, or `--slot N`). Caches the token in `~/.escarp/leases.json`. `--focus` brings the window forward, `--prompt` emits a CUA preamble, `--hold` heartbeats until Ctrl-C then releases. |
| `escarp release {--mine \| --slot N \| --holder NAME \| --token T}` | Token-free release for humans. |
| `escarp scale N` | Resize the pool to N slots. Probes the cdp ports, launches the missing slots below N, terminates the live ones at/above N, persists N, then clean-restarts the daemon. `--dry-run` prints the plan only; `--force` resizes past a leased slot; `--keep-data` keeps removed profiles/bundles; `--no-restart` reconciles without bouncing the daemon. |
| `escarp pool add [N]` | Add one slot to the **running** pool, no daemon restart. Launches the chrome (lowest-free index, or N), then has the live daemon broker it via `POST /pool/add`. `--no-launch` requires a chrome already listening; `--cua-apps/--no-cua-apps` sets the identity mode. Persists the new size. |
| `escarp pool remove N` | Remove one slot from the running pool, no restart. Has the daemon unbroker + drop the lock via `POST /pool/remove`, then kills the chrome and cleans its data. `--force` removes a leased slot; `--keep-chrome` leaves it running; `--keep-data` keeps the profile/bundle. |
| `escarp window <slot>` | Print and live-verify a slot's identity. In CUA app mode this is the per-slot app bundle ID; in OS-window mode it also returns `os_window_id`, owner_pid, bounds, and verification fields. Supports `--json` and `--verify-key`. |
| `escarp focus <slot>` | Best-effort: use the OS-window identity to bring a slot forward (CDP `Page.bringToFront` + `osascript activate` + AX raise), then verify the right window is key. Reports success only when verification passes. |
| `escarp docs [codex-cua]` | List bundled docs and their installed paths, or print the Codex CUA quickstart. |
| `escarp setup {codex \| claude-code}` | Convenience preflight + MCP registration. Useful but not required for the CLI native-CUA flow if `escarp acquire --prompt --hold` works. |

`escarp setup` is a convenience check, not a gate. If it reports an MCP
smoke-test issue but `escarp acquire --prompt --hold` prints a bundle-ID prompt
and the slots are running, the native CUA flow still works — treat setup
failures as "MCP wiring needs attention," not "the pool is broken."

## Resizing the pool

The pool size is the single canonical number for how many slots this machine
runs. It is persisted in `~/.escarp/pool.json` (default 4) so the chromes and
the daemon can never silently disagree: a plain `escarp daemon` restart re-reads
the persisted size instead of falling back to the default and orphaning the
slots above it.

```bash
escarp scale 6            # grow to 6 slots
escarp scale 4            # shrink to 4 (terminates slots 4,5 and their data)
escarp scale 8 --dry-run  # show the plan without touching anything
```

`scale` rebuilds the whole pool and bounces the daemon. To nudge the pool by one
slot *while it keeps serving*, use `pool add` / `pool remove`, which change
membership on the running daemon with no restart:

```bash
escarp pool add          # add the lowest free slot, live
escarp pool add 7        # add a specific slot, live
escarp pool remove 7     # remove it again, live
```

In every case the daemon never owns chrome lifecycles: `pool add` launches the
chrome first, then registers it; `pool remove` unbrokers first, then kills the
chrome. Leases on untouched slots are never interrupted. `ESCARP_POOL_SIZE`
still overrides the persisted size for one-off runs.

## The MCP shim

`escarp setup codex` (or `claude-code`) registers the bundled MCP server. The
model gets three tools — `escarp_acquire`, `escarp_status`, `escarp_release` —
that return **structured identity**, not text:

```json
// escarp_acquire returns:
{
  "slot": 1,
  "cua_app_bundle_id": "dev.escarp.chrome.slot1",
  "cua_app_name": "Escarp Chrome Slot 1",
  "cua_app_path": "/Users/me/.escarp/cua-apps/Escarp Chrome Slot 1.app",
  "cdp_port": 9223,
  "cdp_ws_url": "ws://127.0.0.1:9223/devtools/browser/...",
  "os_window_id": null,
  "expires_at": 1780174370,
  "auto_heartbeat_interval_s": 60.0
}
```

`escarp_status` returns the same shape per slot, and the shim auto-heartbeats to
keep the lease alive while it runs. When the pool is full, `/status` carries
`last_heartbeat`, `expires_at`, `suspected_stale`, `available_after_s`, and
`retry_after_s` per slot, so an agent waits for `retry_after_s` and retries
rather than guessing whether another holder is dead.

## HTTP API

The broker listens on `http://127.0.0.1:7878`.

| Verb | Body | Returns |
| ---- | ---- | ------- |
| `GET  /status` | — | Pool snapshot: slot identity, holder, heartbeat/expiry, `suspected_stale`, bounded retry fields |
| `POST /acquire` | `{"holder": str, "slot"?: int, "dev_port"?: int}` | Lease record with full identity payload |
| `POST /heartbeat` | `{"lease_token": str}` | Refreshed lease |
| `POST /release` | `{"lease_token": str}` | Lease in `state: free` |
| `GET  /reaped` | — | Last 50 TTL-expired reclamations (debug) |
| `POST /pool/add` | `{"slot": int}` | Hot-register a listening chrome, no restart |
| `POST /pool/remove` | `{"slot": int, "force"?: bool}` | Hot-unregister a slot and drop its lock, no restart |

## CDP vs CUA — picking a transport

Both transports act on the same persistent slot. Pick by what you need:

| Property | CUA (Codex Desktop) | CDP (Playwright / chrome-devtools-mcp) |
|---|---|---|
| Visible OS cursor | ✅ moves on screen | ❌ no cursor movement |
| Native OS overlays (file pickers, downloads, password manager) | ✅ fully supported | ❌ DOM only |
| Determinism | screenshot + AX tree per turn | deterministic CDP commands |
| Concurrent multi-slot | ✅ on macOS with `launch-pool --cua-apps` | ✅ true parallel agents |
| Slot targeting | leased per-slot app bundle ID | leased `cdp_ws_url` |
| Best for | end-user-facing tasks, demos, native UI | dev automation, parallel test runs, headless |

## Architecture

**Control plane:** a slot allocator (kernel-flock atomicity), a lease broker
with TTL + reaper, the HTTP API on 7878, the MCP shim, and two identity modes —
per-slot app bundle IDs for native CUA, or OS-window calibration
(`kCGWindowNumber` on macOS) for same-bundle/CDP workflows.

**Data plane:** Codex CUA via OS Accessibility, or Playwright / Chrome DevTools
MCP / any CDP client over the leased `cdp_ws_url`. Escarp provisions and points;
it does not proxy clicks.

The **persistence contract** is the load-bearing trick: chromes are launched
detached (`start_new_session=True`) and reparent to launchd/init. The daemon
discovers them via `/json/version` and never owns their lifecycle. Kill the
daemon, the chromes stay up. Kill an agent mid-task, the reaper reclaims its
lease within one sweep interval (default 2 s). On every release boundary the
broker opens a fresh `about:blank` tab and closes the rest — no state inherits
across holders.

**Lease liveness is broker-owned.** A heartbeat (`POST /heartbeat` with the
secret lease token) refreshes `last_heartbeat` and extends `expires_at`; MCP
shims send it at `TTL/3`, and `escarp acquire --hold` does the same in the
foreground. If a holder stops heartbeating, the slot is *not* stolen — it
becomes reclaimable only when the reaper observes `expires_at` has passed. The
reaper is the only authority that frees an expired lease.

See [`research/cua_targeting.md`](research/cua_targeting.md) for the CUA
addressing analysis.

## Configuration

| Env var | Default | What |
| ------- | ------- | ---- |
| `ESCARP_POOL_SIZE` | `4` | Number of browser slots (overrides `pool.json`) |
| `ESCARP_CDP_BASE` | `9222` | CDP port for slot 0; slot N uses base+N |
| `ESCARP_API_PORT` | `7878` | Broker HTTP API port (bind-and-shift on collision) |
| `ESCARP_LEASE_TTL_S` | `60` | Lease expiry; the reaper reclaims past this |
| `ESCARP_DISCOVERY_WAIT_S` | `0` | Seconds the daemon waits per slot for a chrome to appear at boot |
| `ESCARP_CFT_BINARY` | autodetect | Path to the Chrome for Testing binary |
| `ESCARP_BROKER_URL` | `http://127.0.0.1:7878` | Where the MCP shim looks for the broker |
| `ESCARP_LEASES_FILE` | `~/.escarp/leases.json` | Local cache of lease tokens for `escarp release --mine` |

### Per-slot resource derivation

```
slot s  ->  frontend  = 3000 + s*10
            backend   = 8000 + s*10
            postgres  = 5432 + s*10
            cdp_port  = 9222 + s
            user_data = ~/.escarp/profiles/<tier>/slot-<s>
            cua_app   = dev.escarp.chrome.slot<s>   (when launched with --cua-apps)
            os_window_id = <calibrated at daemon startup, outside CUA app mode>
```

## Demos

```bash
# Visible lockstep concurrency (CDP): two holders, two browsers, both doing work
# at the same wall-clock instants via asyncio.gather. Proves the lease model.
uv run python scripts/demo_two_holders_concurrent.py

# Lease-boundary reset: drive a leased slot to YouTube, release, watch the tab
# snap back to about:blank. Proves no state inherits across holders.
uv run python scripts/demo_reset_on_release.py

# Parallel no-clobber (naive): drive two slots against two sites in parallel and
# screenshot each. Confirms parallel drives don't cross-contaminate.
uv run python scripts/demo_two_agents.py

# Parallel no-clobber (sustained): each slot does 4 interleaved navigations on
# its own domain, screenshotting every step. A clobber shows up as the wrong
# domain in a checkpoint.
uv run python scripts/demo_concurrent_tabs.py
```

Native-CUA flow:

```bash
escarp launch-pool --pool-size 2 --cua-apps
ESCARP_POOL_SIZE=2 escarp daemon
escarp acquire --slot 0 --holder cua-demo --prompt --hold
# paste the printed bundle-ID preamble into Codex Desktop, append a task,
# press Ctrl-C here when done
```

## Status

v1.4.0.

**Holds today:**
- Each native-CUA slot has a stable per-slot app bundle identity on macOS (`dev.escarp.chrome.slotN`).
- Same-bundle slots still have a live-verifiable OS-window identity (macOS); `escarp window <slot>` queries it live.
- Two agents on different slots drive their own CfTs over CDP without colliding.
- Two native Codex CUA agents drive different slots concurrently when the pool is launched with `--cua-apps`.
- Killing an agent returns its browser within one reaper interval.
- Pool exhaustion returns a structured 409, not a hang.

**Does not hold:** two concurrent native CUA agents on two windows of the *same*
app bundle — CUA addresses by app, not window; use `--cua-apps` for native CUA
concurrency.

**Not yet:** delegated and supervised identity tiers, cross-machine pooling,
Linux/Windows OS-window calibration.

## License

MIT
