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
# 1. Spawn N persistent Chrome for Testing windows (one-shot, exits immediately).
#    The chromes are detached and survive escarp restarts.
escarp launch-pool                  # default 4
ESCARP_POOL_SIZE=8 escarp launch-pool

# 2. Start the broker daemon. It discovers the chromes via /json/version,
#    serves the lease HTTP API on 127.0.0.1:7878, and runs a reaper for
#    expired leases. ^C releases the slot locks but leaves chromes alive.
escarp daemon &

# 3. Inspect the pool
curl -s http://127.0.0.1:7878/status | jq
```

Drive one of the leased browsers in Python:

```python
import asyncio, httpx
from playwright.async_api import async_playwright

async def main():
    lease = httpx.post(
        "http://127.0.0.1:7878/acquire",
        json={"holder": "demo-script"},
    ).json()
    print("got slot", lease["slot"], "->", lease["cdp_ws_url"])

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

## Wire it into Claude Code / Codex via MCP

Register the bundled MCP shim so the model gets three first-class tools
(`escarp_status`, `escarp_acquire`, `escarp_release`) and never has to
curl the broker by hand:

```bash
# Claude Code
claude mcp add escarp -- escarp-mcp

# Codex CLI
codex mcp add escarp escarp-mcp
```

Auto-heartbeat lives in the shim, so a long-running session can't lose the
lease mid-task. On disconnect the lease releases and the slot returns to
the pool, reset to about:blank.

## HTTP API (three verbs)

| Verb | Body | Returns |
| ---- | ---- | ------- |
| `GET  /status` | -- | Pool snapshot, no lease tokens leaked |
| `POST /acquire` | `{"holder": str, "slot"?: int, "dev_port"?: int}` | `{slot, cdp_ws_url, lease_token, expires_at, ...}` |
| `POST /heartbeat` | `{"lease_token": str}` | Refreshed lease record |
| `POST /release` | `{"lease_token": str}` | Lease record in `state: free` |
| `GET  /reaped` | -- | Last 50 TTL-expired reclamations (debug) |

## Architecture (one paragraph)

**Control plane (escarp):** slot allocator with kernel-flock atomicity,
lease broker with TTL + reaper, HTTP API on 7878, MCP shim. **Data plane
(your agent's tools):** Playwright `connect_over_cdp`, `@playwright/mcp`
`--cdp-endpoint`, `chrome-devtools-mcp` `--browser-url`, or whatever CDP
client you want, attached directly to the leased `cdp_ws_url`. Escarp
provisions and points; it never proxies your clicks. If escarp ever shows
up in your per-click latency, that's a bug.

The persistence contract is the load-bearing trick: chromes are launched
detached (`start_new_session=True`) and reparent to launchd/init. The
daemon discovers them by GET `/json/version`; it never owns their
lifecycle. Kill the daemon, chromes stay up. Kill an agent mid-task, the
reaper reclaims its lease within one sweep interval (default 2 s). On every
release boundary the broker `PUT /json/new?about:blank`s a fresh tab and
closes the old ones, so no state inherits across holders.

See [V2_PLAN.md](V2_PLAN.md) for the full design and decision record.

## Demos

The repo ships scripts that prove the headline claims end-to-end:

```bash
# Two holders, two browsers, asyncio.gather'd lockstep concurrency.
# Steps fire within ~70 ms across both browsers; ~2x parallel speedup.
uv run python scripts/demo_two_holders_concurrent.py

# Lease-boundary reset: drive to YouTube, release, watch the tab snap back
# to about:blank. Proof that state does not leak across holders.
uv run python scripts/demo_reset_on_release.py
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

## Per-slot resource derivation

Each slot derives all its resources from a single index, so two worktrees
on different slots never collide on ports:

```
slot s  ->  frontend  = 3000 + s*10
            backend   = 8000 + s*10
            postgres  = 5432 + s*10
            cdp_port  = 9222 + s
            user_data = ~/.escarp/profiles/<tier>/slot-<s>
```

## Status

v1.0.0. The four headline claims hold:

- Two agents on different slots drive their own leased CfTs, never collide.
- Killing an agent mid-task returns its browser within one reaper interval.
- Pool exhaustion returns a structured 409, not a hang.
- No lockfiles outside the broker's single source of truth.

What's not in 1.0: delegated and supervised identity tiers (v1.1 and v1.2),
cross-machine pooling (v2), Docker compose orchestration (hooks only for
now -- escarp doesn't own compose semantics).

## License

MIT
