# escarp

**Identity-aware runtime for parallel coding agents.** A single broker daemon
owns a pool of persistent Chrome for Testing windows and hands CDP endpoint
leases to coding agents (Claude Code, Codex) over MCP. N worktrees can run N
agents with N isolated browsers, no stale-lock hell.

> **Why?** Spawn-a-browser-per-tool-call leaks chromes on every chat exit.
> Per-agent lockfiles strand themselves when the agent dies. Driving the
> user's daily-driver browser pollutes cookies and session state. Escarp
> separates lifecycle (persistent, owned by escarp) from leases (ephemeral,
> owned by the agent). The browsers always exist; agents check them out.

## Install

```bash
pip install escarp
```

Requirements: Python 3.11+, a Chrome for Testing binary on disk. Easiest way
to get one:

```bash
npx @puppeteer/browsers install chrome@stable
export ESCARP_CFT_BINARY=".../Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
```

## Quick start

```bash
# 1. Spawn N persistent CfT windows (one-shot; detached; survive escarp restarts).
escarp launch-pool                  # default 4

# 2. Start the broker. ^C releases slot locks but leaves chromes alive.
escarp daemon &

# 3. One-command wire-up for your agent of choice:
escarp setup codex                  # registers escarp-mcp with Codex CLI
escarp setup claude-code            # registers escarp-mcp with Claude Code
```

Lease + drive flow for a human-driven Codex CUA session (single-window, visible
cursor — see the [CUA vs CDP](#cua-vs-cdp-pick-the-right-driver) note below):

```bash
escarp acquire --holder me --focus --prompt
# -> "Paste this into Codex CUA: '...' " + the release hint
# ... drive the leased window via Codex Desktop ...
escarp release --mine
```

Or drive it yourself in Python:

```python
import asyncio, httpx
from playwright.async_api import async_playwright

async def main():
    lease = httpx.post(
        "http://127.0.0.1:7878/acquire",
        json={"holder": "demo-script"},
    ).json()
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(lease["cdp_ws_url"])
        page = browser.contexts[0].pages[0]
        await page.goto("https://example.com")
        await page.screenshot(path="/tmp/example.png")
        await browser.close()  # disconnect; broker-owned chrome stays alive
    httpx.post(
        "http://127.0.0.1:7878/release",
        json={"lease_token": lease["lease_token"]},
    )  # broker auto-resets the tab to about:blank

asyncio.run(main())
```

## CUA vs CDP — pick the right driver

Escarp manages the **persistent windows and their leases**. It does not drive
the browser. Pick whichever driver matches the task:

| Property | Codex CUA (native) | CDP (Playwright / chrome-devtools-mcp) |
|---|---|---|
| Visible OS cursor | ✅ moves on screen | ❌ no cursor movement |
| Native OS overlays (file pickers, downloads, password manager, permission sheets) | ✅ fully supported | ❌ DOM only |
| Determinism | screenshot-and-AX-tree at every turn | deterministic CDP commands |
| Concurrent multi-window | ❌ **one CUA-controlled window per app bundle** | ✅ true parallel agents |
| Targeting | by app bundle + frontmost window | by leased `cdp_ws_url` (window-addressable) |
| Best for | end-user-facing tasks, demos, anything with native UI | dev automation, parallel test runs, headless |

**The CUA concurrency limit is structural**, not an escarp choice: Codex CUA's
addressing model is per-app (selects the *key window* of an app each turn), so
two CUA sessions against two Chrome for Testing windows would both resolve to
"the CfT app → its frontmost window." Whoever focused last wins. The
[`research/cua_targeting.md`](research/cua_targeting.md) report has the static
analysis behind this.

Escarp's job for native CUA work is therefore: **make the right persistent
window the OS-foreground key window before the agent acts**. That's what
`escarp focus <slot>` and `escarp acquire --focus --prompt` do.

## Commands

| Command | Purpose |
|---|---|
| `escarp launch-pool` | Spawn N detached Chrome for Testing windows. One-shot; chromes outlive this command. Idempotent (skips slots already listening). |
| `escarp daemon` | Discover the live chromes, broker leases on `127.0.0.1:7878`, run the reaper. Does NOT own chrome lifecycles — ^C releases locks but leaves chromes alive. |
| `escarp setup codex` | Preflight (CfT, daemon, pool, MCP path, codex CLI), register `escarp-mcp` with Codex, run end-to-end smoke test. Idempotent. |
| `escarp setup claude-code` | Same shape, for Claude Code. CDP path (Claude has no native CUA yet). |
| `escarp focus <slot>` | Bring slot N's CfT window to the OS foreground. Three layers: CDP `Page.bringToFront`, macOS `osascript activate`, AX per-window promote-by-title. Title the window `escarp-slot-N` along the way. |
| `escarp acquire --holder X [--focus] [--prompt]` | Lease a slot. With `--focus`, brings the window forward. With `--prompt`, prints a paste-ready Codex CUA preamble that targets the focused window. Persists the lease token to `~/.escarp/leases.json` so you don't need to remember it. |
| `escarp release {--slot N \| --holder NAME \| --mine \| --token T}` | Release leases. `--mine` releases every lease this machine recorded. Token-free for humans. |

## Wire it into agents via MCP

The setup commands above are the easy path. If you want to register manually:

```bash
# Claude Code
claude mcp add escarp -- escarp-mcp

# Codex CLI
codex mcp add escarp -- escarp-mcp
```

The model gets three first-class tools (`escarp_status`, `escarp_acquire`,
`escarp_release`). Auto-heartbeat lives in the shim — a long session can't lose
the lease mid-task. On disconnect the lease releases and the slot resets to
about:blank.

## HTTP API (three verbs)

| Verb | Body | Returns |
| ---- | ---- | ------- |
| `GET  /status` | -- | Pool snapshot, no lease tokens leaked |
| `POST /acquire` | `{"holder": str, "slot"?: int, "dev_port"?: int}` | `{slot, cdp_ws_url, lease_token, expires_at, ...}` |
| `POST /heartbeat` | `{"lease_token": str}` | Refreshed lease record |
| `POST /release` | `{"lease_token": str}` | Lease record in `state: free` |
| `GET  /reaped` | -- | Last 50 TTL-expired reclamations (debug) |

## Architecture (one paragraph)

**Control plane (escarp):** slot allocator with kernel-flock atomicity, lease
broker with TTL + reaper, HTTP API on 7878, MCP shim, focus primitive. **Data
plane (your driver):** Codex CUA via OS Accessibility, or Playwright / Chrome
DevTools MCP / any CDP client over the leased `cdp_ws_url`. Escarp provisions
and points; it does not proxy clicks. If escarp ever shows up in your per-click
latency, that's a bug.

The persistence contract is the load-bearing trick: chromes are launched
detached (`start_new_session=True`) and reparent to launchd/init. The daemon
discovers them by GET `/json/version`; it never owns their lifecycle. Kill the
daemon, chromes stay up. Kill an agent mid-task, the reaper reclaims its lease
within one sweep interval (default 2 s). On every release boundary the broker
`PUT /json/new?about:blank`s a fresh tab and closes the old ones — no state
inherits across holders.

See [V2_PLAN.md](V2_PLAN.md) for the full design and decision record.

## Demos

```bash
# Two holders, two browsers, asyncio.gather'd lockstep concurrency (CDP).
# Steps fire within ~70 ms across both browsers; ~2x parallel speedup.
# This is the CDP path -- proves the lease/concurrency model.
uv run python scripts/demo_two_holders_concurrent.py

# Lease-boundary reset: drive to YouTube, release, watch the tab snap back
# to about:blank. Proof that state does not leak across holders.
uv run python scripts/demo_reset_on_release.py
```

Native-CUA demo flow (single-window, visible cursor):

```bash
escarp acquire --holder cua-demo --focus --prompt
# paste the printed preamble into Codex Desktop, append a task
# observe the visible Codex CUA cursor act in the focused CfT window
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
```

## Status

v1.1.0.

**Claims that hold:**
- Two agents on different slots drive their own leased CfTs over **CDP**, never collide.
- Killing an agent mid-task returns its browser within one reaper interval.
- Pool exhaustion returns a structured 409, not a hang.
- No lockfiles outside the broker's single source of truth.
- Single-window native Codex CUA flow: `escarp focus <slot>` makes the right window key, then a CUA prompt targeting the frontmost CfT window acts there with the visible OS cursor.

**Claims that do NOT hold (and aren't claimed):**
- Two concurrent native Codex CUA agents on two CfT windows. Not supported today — CUA addresses by app, not window. Use CDP for concurrent multi-agent work; use CUA for single-window high-fidelity work.

**Not in 1.1:** delegated and supervised identity tiers (v1.2+), cross-machine pooling, Docker compose orchestration.

## License

MIT
