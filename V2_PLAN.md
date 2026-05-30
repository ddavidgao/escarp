# Escarp — Planning Document (v2)

> **For Claude Code:** Read this document top to bottom before writing any code.
> When you've read it, respond with (1) what you think is wrong or underspecified,
> (2) what questions you need answered before starting, and (3) a proposed
> technical design for the v2 MVP. Do not begin implementation until we've
> discussed the design.

> This document supersedes `PLAN.md` for v2. v0 (`pip install escarp==0.1.0`)
> targeted a single API (`escarp.run(task=...)`) that picked a Playwright
> context per task. v2 inverts the shape: a long-lived daemon owns a pool of
> persistent browsers and hands leases to coding agents over MCP. The
> identity-tier model from v0 lives on as the *lease policy* attached to a slot.

## 1. What v2 is

Escarp v2 is a **single control-plane daemon** that:

1. Allocates localhost ports and Docker stacks across N git worktrees.
2. Maintains a pool of N persistent Chrome for Testing browsers, one per slot,
   parented to the daemon (never to an agent).
3. Hands **CDP endpoint leases** to coding agents (Claude Code, Codex CLI) over
   MCP, then gets out of the way.
4. Reaps stale leases via TTL + heartbeat, never via lockfiles the agent owns.

The one-line pitch (v2): **"identity-aware runtime for parallel coding agents —
N worktrees, N browsers, one broker, zero stale locks."**

The v0 thesis ("escarp picks the right mode of web access per task") generalizes
cleanly: **the lease *is* the mode.** A slot's `user-data-dir`, reset policy,
network policy, and signed identity together encode the v0 mode triad
(autonomous / delegated / supervised) as a per-lease attribute.

## 2. Core principle: control plane vs data plane

- **Control plane (escarp):** allocate ports, manage Docker, launch/lease/reap
  browsers, own all state. Touched only at `acquire`, `heartbeat`, `release`.
- **Data plane (the agent's MCP browser server):** chrome-devtools-mcp or
  @playwright/mcp talks CDP **directly** to the leased browser. Escarp is NOT
  in the per-click path.

If escarp ever proxies a browser action, it's a bug. Provision and point,
never drive.

## 3. Lifetimes — the persistence contract (read twice)

Two independent lifetimes. Decoupling them is the whole point.

- **Browser process lifetime — PERSISTENT, owned by the broker daemon.** The
  daemon launches the N Chrome for Testing windows once and keeps them alive
  across every chat, agent, and acquire/release cycle. They die only when you
  tear down the pool. An agent never owns, spawns, or closes a browser process.
- **Lease lifetime — EPHEMERAL.** An agent acquires a lease on an already-running
  window, drives it, releases. On release (or TTL expiry, or disconnect) the
  lease returns to `free` and the window is *reset* (close stray tabs, navigate
  back to its dev port). The **process is never touched.** Chat ends → lease
  freed → window keeps running, idle, ready for the next agent.

Hard rule: **the agent must never be the parent process of a browser.** If a
tool subprocess spawns Chrome as a child, Chrome dies when that subprocess
exits — exactly the close-on-chat-end behavior we must avoid. Browsers are
launched detached and parented to the long-lived daemon only.

## 4. Locked decisions (do not re-litigate)

1. **Browser = Chrome for Testing, version-pinned** in `pyproject.toml`. One
   binary, deliberate updates only. Never automate the daily-driver browser.
   Chromium-family only (CDP requirement).
2. **CDP is the lingua franca.** One browser per slot,
   `--remote-debugging-port` + own `--user-data-dir`. Agents attach via the
   standard browser MCP servers (chrome-devtools-mcp `--browser-url` or
   @playwright/mcp `--cdp-endpoint`). We do **not** build a CDP client.
3. **One broker = single source of truth.** No per-agent lockfiles anywhere.
   Scattered lockfiles with no liveness signal are the root cause of
   stale-lock hell. Kill them.
4. **The LLM never holds a lock.** Acquire/renew/release happen in
   deterministic plumbing inside the MCP shim, invisible to the model's
   reasoning. The model only ever asks "give me a browser."
5. **Lease liveness = observable, never intentional.** A lease is held iff
   (TTL not expired AND heartbeat current) OR (the agent's MCP connection is
   still open). Disconnect = release **the lease**, NOT close the browser.
6. **Ports: bind-and-shift, never check-then-bind.** Attempt to bind/publish;
   on EADDRINUSE, shift by 10 and retry. `bind()` is the atomic test. No
   pre-check, no lock.
7. **Everything is keyed by slot index (0..N-1).** Slot derives dev ports AND
   browser identity.
8. **Escarp v2 ignores Codex's internal `cdp` browser backend** (closed-source,
   not user-exposed as of mid-2026 — see Phase 0 findings below). Both Codex
   CLI and Claude Code attach to the broker's leased browsers via standard
   MCP browser servers. Symmetric design without depending on OpenAI's
   internal backend selection.

## 5. Resource model

Per worktree, claim ONE slot atomically (flock a per-slot lockfile, hold the
fd for process lifetime — kernel auto-releases on death). Derive everything
from the slot:

```
slot s  ->  frontend  = 3000 + s*10
            backend   = 8000 + s*10
            postgres  = 5432 + s*10   (prefer: keep DB internal to a per-project docker network)
            cdp_port  = 9222 + s
            user_data = ~/.escarp/profiles/<tier>/slot-<s>
```

Broker lease record (single source of truth, one per browser slot):

```
slot:           <int>
state:          free | leased
tier:           autonomous | delegated | supervised   (the v0 mode lives here)
holder:         <agent id, e.g. cc-worktree-3>        (null if free)
dev_port:       <int the browser is pointed at>        (null if free)
cdp_ws_url:     ws://127.0.0.1:<cdp_port>/devtools/browser/<id>
acquired_at:    <ts>
expires_at:     <ts>   # past now() => stale => reaper frees it
last_heartbeat: <ts>
```

## 6. Identity tiers as lease policies (the v0 bridge)

v0's three modes become lease policies attached at acquire time:

| v0 Mode | v2 Lease policy |
|---------|-----------------|
| Autonomous (signed self-id) | `tier=autonomous`. User-data-dir under `profiles/autonomous/slot-<s>`. Reset-on-release closes tabs + nav to dev_port; keeps Ed25519 identity material. Outbound HTTP signs with Web Bot Auth via injected proxy. |
| Delegated (scoped OAuth) | `tier=delegated`. User-data-dir under `profiles/delegated/slot-<s>`. OAuth token injected at lease time as cookie or header proxy. Reset-on-release wipes the OAuth context but keeps the profile shell. |
| Supervised (human-gated session inherit) | `tier=supervised`. **Process-per-identity**, not context-per-identity (per the original plan's Escarp hook). Dedicated persistent CfT per supervised identity. Reset-on-release does NOT clear cookies/storage — the human grants long-lived session inherit. |

A single broker can run multiple pools (one per tier). For v2-MVP, scope to
a single autonomous pool; delegated and supervised are v2.1 / v2.2.

## 7. Phases

### Phase 0 — Codex CDP attach de-risk (RESOLVED — see § Phase 0 findings)

**Outcome:** asymmetric internal-backend selection is irrelevant; both agents
attach via standard MCP browser servers. Symmetric data plane achieved.

### Phase 1 — Slot + port allocator (`src/escarp/broker/slots.py`)

- flock-per-slot claim; hold fd for process lifetime.
- Port assignment derived from slot.
- Bind-and-shift fallback only if a derived port is occupied by a foreign
  process.
- No locks beyond the single slot flock.

### Phase 2 — Browser provisioner (`src/escarp/broker/browser.py`)

- Replaces v0's `workspace/chromium.py`. The Playwright-managed
  `launch_persistent_context` model is wrong for the pool — Playwright wants
  to own the lifecycle. We launch CfT directly via `subprocess.Popen` detached
  (`start_new_session=True`, no parent file descriptors leaked).
- **Discovery: `GET http://127.0.0.1:<cdp_port>/json/version`** (primary).
  Returns the full handshake including `webSocketDebuggerUrl`. *Plan
  correction from Phase 0 smoke test:* the original plan's claim that
  Chrome 144+ removed `/json/version` is empirically false — verified
  working in CfT 149.0.7827.54 on macOS arm64. The `DevToolsActivePort`
  file in the user-data-dir does NOT appear (also verified in the same
  smoke test), so file-based discovery is the wrong primary mechanism.
  Fallback: parse `DevTools listening on ws://...` from Chrome's stderr
  during launch.
- Launch flags (locked):
  ```
  --remote-debugging-port=<cdp_port>
  --user-data-dir=<profile_dir>
  --no-first-run
  --no-default-browser-check
  --disable-backgrounding-occluded-windows
  --disable-renderer-backgrounding
  --disable-background-timer-throttling
  --disable-extensions
  ```
- Headed and parked is the intended mode. CDP input injection
  (`Input.dispatchMouseEvent`) does not move the real cursor or steal OS
  focus, so an agent can drive a parked window while you keep working
  elsewhere.
- **Self-heal:** if a browser process dies (crash, not an agent), the daemon
  detects the dead CDP connection and relaunches on the same
  slot/port/user-data-dir.
- **On lease release, reset — never close.** Close stray tabs via CDP
  `Target.closeTarget`, navigate the remaining tab to `http://localhost:<dev_port>`.
  Keep the profile.

### Phase 3 — Lease broker (`src/escarp/broker/lease.py`)

- `acquire(slot|any, dev_port, tier, on_empty)`:
  - free browser in matching pool exists -> lease, point at dev_port, return
    `{cdp_ws_url, dev_port, lease_token, ttl}`.
  - none free + on_empty=wait -> block up to N s, retry, else report.
  - none free + on_empty=report -> return structured pool status so the agent
    can escalate to the human.
- `heartbeat(lease_token)` -> extends expires_at. Called automatically by the
  MCP shim on every browser interaction + a background timer. The model never
  calls this.
- `release(lease_token)` -> frees immediately. Also: on MCP client disconnect,
  free all its leases.
- **Reaper:** background asyncio task sweeping every 2s; any lease with
  `expires_at < now()` -> free, logged.

### Phase 4 — MCP shim (`src/escarp/broker/mcp.py`)

Expose exactly three verbs to the agent: `acquire`, `status`, `release`.
Nothing that relays a click. `acquire` returns the CDP handle; the agent's
own browser MCP server (chrome-devtools-mcp / @playwright/mcp) consumes it.

Auto-heartbeat lives in the shim, not the model. Implemented via
[fastmcp](https://github.com/jlowin/fastmcp) or the official `mcp` SDK.

### Phase 5 — Adapters (configuration, not code)

- **Codex CLI:** `codex mcp add` to register chrome-devtools-mcp pointed at
  the broker's leased `cdp_ws_url`. Or register the escarp MCP shim itself
  to handle acquire/release transparently.
- **Claude Code:** `claude mcp add` (or `~/.claude.json`) to register
  @playwright/mcp `--cdp-endpoint <leased_ws>` or chrome-devtools-mcp
  `--browser-url http://127.0.0.1:<cdp_port>`.

We do not rebuild these. We document the exact registration commands in the
README.

### Phase 6 — Visibility (`src/escarp/cli.py` extends)

- `escarp status` — prints the full lease table.
- `escarp watch` — `watch`-able dashboard (curses or rich.Live) reading the
  broker. Shows who holds what, since when, expiry, which dev_port, and
  which leases are about to be reaped.

## 8. v0 → v2 file map

| v0 file | v2 fate |
|---------|---------|
| `src/escarp/agent.py` | Drop. v0 task-running shape doesn't survive. |
| `src/escarp/cli.py` | Rewrite. New verbs: `daemon`, `status`, `watch`, `slot`. |
| `src/escarp/config.py` | Keep + extend. Add pool-size, tier configs. |
| `src/escarp/identity/keypair.py` | Keep. Still relevant for autonomous tier. |
| `src/escarp/identity/signing.py` | Keep. Web Bot Auth still relevant. |
| `src/escarp/modes/{autonomous,delegated,supervised}.py` | Refactor to lease policies, not task runners. |
| `src/escarp/router.py` | Rewrite as slot selector inside broker. |
| `src/escarp/run.py` | Drop. No `escarp.run(task=...)` API in v2. |
| `src/escarp/workspace/base.py` | Drop. v0 BrowserPage protocol redundant. |
| `src/escarp/workspace/chromium.py` | Replace with `broker/browser.py`. |
| `src/escarp/workspace/lightpanda.py` | Drop. Doesn't fit the pool model. |
| `tests/*` | Mostly rewrite. Keep identity tests. |

New modules:

```
src/escarp/broker/
  __init__.py
  slots.py        # Phase 1 (flock + port derivation)
  browser.py      # Phase 2 (CfT launcher + reset)
  lease.py        # Phase 3 (acquire/release/reaper)
  mcp.py          # Phase 4 (MCP shim verbs)
  daemon.py       # asyncio entry point, owns all state
  state.py        # in-memory state + observers
```

## 9. Phase 0 findings (resolved)

Original plan flagged Phase 0 as "do this first": confirm Codex CU can attach
to an externally launched Chrome via CDP. Desk research confirms the
**internal `cdp` backend path is closed and not exposed**:

- The OSS `codex-rs` CLI has zero browser-driving code. `browser_use` and
  `browser_use_external` are feature *gates* in `features/src/lib.rs` — UI
  surface flags, no implementation.
- Browser Use lives entirely in Codex Desktop (closed-source). Three plugin
  manifests at `/Applications/Codex.app/Contents/Resources/plugins/openai-bundled/plugins/`:
  - `browser/` — in-app browser (iab) for localhost workflows.
  - `chrome/` — drives user's Chrome via Codex Chrome extension (id
    `hehggadaopoacecdllhhajmbjkdcmajg`) + native-messaging host
    `com.openai.codexextension`. Native host not bundled in current Desktop
    install.
  - `computer-use/` — macOS-wide CU via Accessibility, separate
    `SkyComputerUseClient.app`.
- Per [openai/codex#20642](https://github.com/openai/codex/issues/20642),
  exposing the `cdp` backend to user config is the ticket's whole purpose
  and is not done as of mid-2026.

**Resolution:** Don't fight closed-source backend selection. Route both agents
through standard browser MCP servers attached to the broker's CDP endpoint:

- `chrome-devtools-mcp@1.1.1`: `--browser-url http://127.0.0.1:<port>` or
  `--ws-endpoint ws://...`
- `@playwright/mcp@0.0.75`: `--cdp-endpoint ws://...`

Codex CLI registers MCP servers via `codex mcp add`. Claude Code via
`claude mcp add`. Symmetric design achieved at the MCP layer, not the
internal-backend layer.

## 10. Open questions for David

**Decided (this session):**

- **Q4 — Daemon API transport.** HTTP on fixed localhost port `7878` (with
  bind-and-shift on collision). Curl-debuggable; MCP shim is a thin client.
- **Q6 — v0 compatibility.** Drop v0 entirely. Clean break. No
  `escarp.run(task=...)` wrapper in v2.
- **Q7 — Version bump.** `1.0.0`. Major version matches the architectural
  break; signals stability for PyPI/X.

**Still open (non-blocking for Phase 1; sensible defaults applied):**

1. **Pool size N.** Default `4`, configurable via `escarp.toml`
   (`[broker] pool_size = N`). Revisit if usage shows 4 is wrong.
2. **Single pool vs per-tier pools.** v2-MVP: single autonomous pool.
   Delegated and supervised tiers land in v2.1 / v2.2.
3. **Daemon lifecycle.** v2-MVP supports manual start via `escarp daemon`
   (interactive, log to stdout) and `escarp daemon --background` (writes
   pidfile + logs to `~/.escarp/daemon.log`). Ship a launchd plist
   template in `docs/launchd/` but don't auto-install. User decides
   supervisor.
5. **State store.** Pure in-memory for v2-MVP. SQLite revisit if we see a
   real crash-recovery need; the reaper sweep already handles orphan
   leases on restart.
8. **Docker stack management scope.** Hooks only for v2-MVP. Broker exposes
   pre-launch / post-launch lifecycle hooks per slot; user wires their
   own `docker compose up -f compose.<slot>.yml`. Escarp doesn't own
   compose semantics.

## 11. Acceptance criteria for v2 MVP

- Two worktrees, two agents (one Codex CLI, one Claude Code), each driving
  its leased CfT pointed at its own dev_port via its MCP browser server. No
  cross-talk.
- Kill an agent mid-task → reaper reclaims its lease within one interval
  (target: ≤5s). No manual cleanup. No stale lock survives past TTL.
- Pool exhaustion produces a structured "all in use / wait or free one"
  response, not a hang.
- No lockfiles exist outside the single broker's state. No locking logic is
  visible to the LLM.
- Fresh-venv DX (per the V1 deltavision lesson): `pip install escarp` →
  `escarp daemon &` → `claude mcp add ...` → working in under 5 minutes.
  Include a smoke test that runs this from `/tmp` with no source-tree
  access.

## 12. Out of scope for v2 MVP

- Cross-machine pooling (broker → broker over Tailscale). v3 idea.
- Identity-tier policies beyond autonomous. v2.1 and v2.2.
- Docker compose orchestration semantics. v2 exposes lifecycle hooks only.
- Fencing tokens (the plan's optional safeguard). Skip for local 8-agent
  scale; revisit if cross-talk is observed.

## 13. Gotchas (bake these in)

- ~~Chrome 144+ has no `/json/version` HTTP endpoint — file-based discovery
  only (`DevToolsActivePort` in user-data-dir).~~ **Plan correction
  (Phase 0):** CfT 149 *does* serve `/json/version`. The
  `DevToolsActivePort` file does NOT appear in macOS profile dirs in our
  testing — use the HTTP endpoint or stderr parse instead.
- CDP serves on 127.0.0.1 only.
- **CDP does not enforce exclusivity** — two clients can connect to the same
  port and stomp each other. The lease is enforced by the broker (MCP shim
  layer), not the browser. The browser will never stop a second attach.
- A short-lived tool call must not own the lease, or the agent loses the
  browser between calls. The lease holder is the per-worktree shim/daemon
  (lifetime ~ the dev session), not the call.
- Playwright's `launch_persistent_context` wants to own Chrome's lifetime.
  We can't use it for pool-managed CfT. Drive CfT directly via
  `subprocess.Popen(..., start_new_session=True)` and detach.
