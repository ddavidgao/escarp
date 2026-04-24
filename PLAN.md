# Escarp — Planning Document (v0)

> **For Claude Code:** Read this document top to bottom before writing any code.
> When you've read it, respond with (1) what you think is wrong or underspecified,
> (2) what questions you need answered before starting, and (3) a proposed
> technical design for the v0 MVP. Do not begin implementation until we've
> discussed the design.

## 1. What Escarp is

Escarp is a Python package that routes AI agents to the right mode of web access
for each task — autonomous, delegated, or supervised — automatically, with
identity isolation and session isolation built in.

The one-line pitch: **"an identity-aware runtime that picks the right mode of
web access per task, so agents never contaminate the user's personal browser."**

Escarp is about *where and how* the agent touches the web. Observation —
how the agent processes what it sees — is the user's concern. Escarp integrates
cleanly with any observation layer (DeltaVision, raw screenshots, DOM snapshots,
vision models) but depends on none of them.

## 2. Who this is for

The author (David Gao) uses it first, for his own agent tasks (research,
monitoring, aggregation). It ships as MIT-licensed open source on PyPI. Users
bring their own Anthropic API key. There is no hosted component, no accounts,
no billing, no SLA, no support guarantee. This is a personal tool published
publicly, not a product.

## 3. The three modes (core abstraction)

Escarp's defining feature is that it presents one API but routes to three
different backends depending on the task:

**Mode A — Autonomous.** The agent acts as itself. It has its own Ed25519
keypair, its own ephemeral browser workspace, optional service accounts. It
signs outbound HTTP requests with Web Bot Auth so Cloudflare-class defenses
recognize it as verified. Never touches user accounts.
*Use case:* public web research, scraping, aggregation.

**Mode B — Delegated.** The agent acts on the user's behalf via OAuth. The user
completes a consent flow once (which may include MFA) and the agent gets a
scoped refresh token stored in the OS keyring. Tokens are user-revocable.
*Use case:* Gmail, Calendar, Drive, GitHub, anything with a proper API.

**Mode C — Supervised.** The agent rides a fork of the user's authenticated
browser session, with human-in-the-loop gates at every consequential action.
The user's password is never stored by Escarp. The user taps Touch ID / Duo /
Windows Hello / approves a prompt when the agent hits an auth wall or needs to
perform a write action.
*Use case:* Brightspace, banking portals, legacy enterprise systems, anything
without OAuth.

The **router** is what makes this interesting. The developer writes one line:

```python
escarp.run(task="summarize my Brightspace deadlines and email me a digest")
```

The router decides: "Brightspace" → Mode C. "email" → Mode B (Gmail OAuth).
"summarize" → just an LLM call. Then it executes each subtask in the right
mode, with correct isolation between them.

## 4. What Escarp is NOT

- Not a browser. It uses existing browsers (Lightpanda, Chromium) as backends.
- Not a scraping framework. It composes with existing ones.
- Not an observation layer. Users bring their own (DeltaVision, screenshots, etc.).
- Not a SaaS. No hosted component planned.
- Not a SOC2-compliant production enterprise product. It's personal infra.
- Not a replacement for Browser Use, Stagehand, Skyvern, etc. It sits below them.
- Not a browser extension or a UI product. It's a Python package.

## 5. What Escarp builds on

**Lightpanda** (https://github.com/lightpanda-io/browser) is the fast path
headless browser. Written in Zig, 11× faster and 9× lighter than Chrome,
speaks CDP. Driven via Python through `chrome-devtools-protocol` bindings.
Falls back to Playwright+Chromium when Lightpanda coverage is insufficient.

**Web Bot Auth** (`draft-meunier-web-bot-auth-architecture-05`, Cloudflare
reference impl at github.com/cloudflare/web-bot-auth) is how Mode A agents
identify themselves. Ed25519 signing on outbound requests, public key
published at a `.well-known` URL on the author's domain (davidgao.com).
Testable against `crawltest.com/cdn-cgi/web-bot-auth`.

**OAuth 2.1 + dual-identity extension**
(`draft-oauth-ai-agents-on-behalf-of-user-02`) is how Mode B works. Standard
authorization code + PKCE for most services today; the agent-actor extension
is added where servers support it (few do yet). Tokens stored in OS keyring.

**macOS Keychain / Windows Credential Manager / Secret Service** (via Python
`keyring` package) is the credential store for Mode B refresh tokens and Mode
C session artifacts. The OS handles biometric challenges (Touch ID, Windows
Hello).

**Chrome profile copy-on-write** is how Mode C works. A fork of the user's
Chrome profile directory is launched with Chromium in an isolated user-data-dir.
Read-only cookie access, write-isolated everything else.

**Claude Opus 4.7** (via Anthropic API) is the router's brain. The mode
selection is not hardcoded rules — it's Claude reasoning about the task and
picking the mode.

## 6. v0 scope (the MVP)

v0 ships only Mode A, because it's the easiest and most self-contained. Modes
B and C come in v0.1 and v0.2. This is important — do not try to ship all
three modes at once. v0 proves the architecture; later versions fill it in.

**v0 success criterion:** an agent completes a predefined task that requires
passing through Cloudflare bot verification, where an unauthenticated Playwright
run against the same task fails or is blocked. Binary, reproducible, demonstrates
the actual thesis. If you can't pass this, v0 isn't done; if you can, v0 ships.

**v0 must include:**

1. Package scaffolding: `pip install escarp`, clean public API, typed with
   pyright/mypy, tested with pytest.
2. `AgentWorkspace` primitive (Mode A implementation): ephemeral profile dir,
   generated Ed25519 keypair, Playwright+Chromium only (Lightpanda is v0.1).
3. Web Bot Auth signing: RFC9421 HTTP message signatures on outbound requests.
   Signature-Agent / Signature-Input / Signature headers per the IETF draft.
   Implemented directly with `cryptography` package — no dependency on
   `http-message-signatures` lib (see Section 9).
4. Key directory server: a small FastAPI app that serves JWKS at
   `/.well-known/http-message-signatures-directory`. Ships as
   `escarp[directory]` optional extras install. Runs locally for dev,
   deploys to davidgao.com for production.
5. Router stub: accepts a task, returns `RouteDecision(mode=Mode.AUTONOMOUS,
   reason="v0: router always selects Mode A")`. Signature matches the final
   router's interface so v0.3's real router is a drop-in.
6. End-to-end demo task: `llms.txt` discovery and fetch against a known-good
   target that publishes one. Demonstrates signed Mode A identity, stable and
   verifiable result.

**v0 explicitly does NOT include:**

- Mode B (OAuth). That's v0.1.
- Mode C (supervised session inherit). That's v0.2.
- The real router (Claude picking modes). v0 just hardcodes Mode A.
- Lightpanda. v0 is Playwright+Chromium only; Lightpanda is v0.1.
- Any observation layer. Users wire in DeltaVision or anything else themselves.
- A TypeScript SDK. Python only.
- A TUI / web UI for HITL prompts. CLI only.
- Stealth / anti-detection features. Not in scope ever.
- Hosted components.

## 7. Proposed architecture (Claude Code: critique this)

```
escarp/
├── pyproject.toml
├── README.md
├── LICENSE                       # MIT
├── PLAN.md                       # this document
├── src/
│   └── escarp/
│       ├── __init__.py           # public API: Agent, run, arun, etc.
│       ├── router.py             # task → RouteDecision (stubbed in v0)
│       ├── modes/
│       │   ├── __init__.py
│       │   ├── autonomous.py     # Mode A
│       │   ├── delegated.py      # Mode B (stub: raises NotImplementedError)
│       │   └── supervised.py     # Mode C (stub: raises NotImplementedError)
│       ├── workspace/
│       │   ├── __init__.py
│       │   ├── chromium.py       # Playwright+Chromium (v0)
│       │   ├── lightpanda.py     # stub: raises NotImplementedError (v0.1)
│       │   └── profile.py        # ephemeral profile mgmt
│       ├── identity/
│       │   ├── __init__.py
│       │   ├── keypair.py        # Ed25519 gen + storage
│       │   └── signing.py        # RFC9421 message signatures (hand-rolled)
│       ├── directory/            # optional extras: escarp[directory]
│       │   ├── __init__.py
│       │   ├── server.py         # FastAPI JWKS endpoint
│       │   └── cli.py            # `escarp-directory` console script
│       └── config.py
├── tests/
│   ├── test_workspace.py         # includes profile isolation tests
│   ├── test_signing.py           # RFC9421 vectors + crawltest.com
│   └── test_router.py
├── bench/
│   └── tasks/                    # reproducible benchmark tasks
├── examples/
│   ├── llms_txt_fetch.py         # v0 demo task
│   └── hello_escarp.py
└── scripts/
    └── verify_web_bot_auth.py    # tests against crawltest.com
```

Key differences from original proposal: `observation/` and `credentials/`
modules removed (not v0 scope). `directory/` promoted to top-level module,
not nested inside `identity/` (different deployment concern). `lightpanda.py`
present as a stub, not absent.

## 8. Public API sketch (Claude Code: critique this too)

```python
from escarp import Agent, run

# High-level one-shot — sync facade, internally uses asyncio.run()
result = run(task="fetch llms.txt from anthropic.com")

# Async variant for callers already in an async context
result = await agent.arun(task="fetch llms.txt from anthropic.com")

# Lower-level for control
agent = Agent(
    name="researcher",
    # mode=... omitted = router decides; explicit override allowed
)
result = agent.run(task="fetch llms.txt from anthropic.com")

# Direct workspace access for advanced users
# observe() is the user's responsibility — they wire in whatever they want
with agent.workspace() as ws:
    ws.goto("https://example.com")
    ws.click("button.search")
    screenshot = ws.screenshot()  # PIL Image — user passes to DeltaVision etc.
    page = ws.page                # underlying Playwright Page if user needs it
```

`ws.goto()` returns None (navigation side-effect, not a new object).
`ws.page` exposes the raw Playwright Page for users who want direct access
or want to plug in an observation layer.

## 9. Stack

- **Language:** Python 3.11+
- **Package manager:** `uv`
- **Build:** `hatchling` via pyproject.toml
- **Browser:** Playwright+Chromium (v0); Lightpanda stub present, activated in v0.1
- **HTTP signing:** hand-rolled RFC9421 implementation (~80 lines) using
  `cryptography` package directly. The `http-message-signatures` PyPI package
  is unmaintained and doesn't handle `Signature-Agent` or derived components
  correctly. Own the security-critical path.
- **Crypto:** `cryptography` package (Ed25519)
- **Key directory server:** FastAPI + uvicorn (optional extras: `escarp[directory]`)
- **Testing:** pytest + pytest-asyncio
- **Typing:** mypy strict mode
- **Linting:** ruff
- **LLM:** Anthropic API, `anthropic` SDK, claude-opus-4-7 as default model
- **Observation:** none — user-supplied

## 10. Design principles (non-negotiable)

1. **User brings their own keys.** Never bundle API keys. `ANTHROPIC_API_KEY`
   env var, clear documentation.
2. **Nothing phones home.** No telemetry. No analytics. No usage reporting.
3. **Credentials never written to disk in plaintext.** OS keyring only.
4. **Session isolation is load-bearing.** Mode A workspaces must not be able
   to read the user's real Chrome profile. Ever. Test this explicitly.
5. **Fail loud, not silent.** If a backend is unavailable, raise a clear
   exception; don't silently fall back. Fallback is opt-in and explicit.
6. **Tests cover security properties, not just happy paths.** The
   profile-isolation test is more important than the "it navigates to a URL"
   test.
7. **No magic.** The router's decision is observable and explainable. If
   Escarp picks Mode A, it logs why. Users who want to override can.
8. **Observation is the user's concern.** Escarp provides `ws.screenshot()`
   and `ws.page` as escape hatches. It does not process, classify, or
   interpret what the agent sees.

## 11. Day-by-day v0 build plan

Not a deadline, a sequencing. Claude Code may propose compression or
reorganization.

**Day 1:** Package scaffolding (uv init, pyproject, ruff, mypy, pytest).
`AgentWorkspace` primitive with Playwright+Chromium. Ephemeral profile
directory with isolation tests: workspace cannot read real profile cookies,
localStorage, extensions, or saved passwords.

**Day 2:** Ed25519 keypair generation. Hand-rolled RFC9421 HTTP message
signing. Unit tests with known test vectors.

**Day 3:** Key directory FastAPI app (`escarp[directory]`). JWKS serving.
Deploy script for davidgao.com. End-to-end test against
crawltest.com/cdn-cgi/web-bot-auth — expect HTTP 200.

**Day 4:** Mode A end-to-end: agent launches workspace, signs outbound
requests, completes a multi-step task against a Cloudflare-protected target.
Integration test verifies: same task fails without signing, succeeds with it.

**Day 5:** Router stub. `RouteDecision` dataclass. Hardcoded Mode A for v0,
structured so v0.3's real router is a drop-in.

**Day 6:** `llms.txt` demo task end-to-end. `hello_escarp.py` example.
Verify the v0 success criterion (signed agent passes, unsigned Playwright
fails).

**Day 7:** README, examples, ship to PyPI as `0.1.0`.

## 12. Longer-term sequence (post-v0)

- **v0.1:** Lightpanda substrate. Mode B (OAuth flows, keyring integration,
  Gmail + Calendar as first-class supported services).
- **v0.2:** Mode C. Chrome profile copy-on-write, HITL prompt system (CLI
  first, maybe a menubar app later), Brightspace as reference implementation.
- **v0.3:** Real router. Claude Opus 4.7 reasoning about mode selection with
  a small classifier for the obvious cases.
- **v0.4:** `llms.txt` / MCP endpoint preferencing — check for machine-first
  interfaces before scraping DOM.
- **v1.0:** When all three modes are stable, a real router is shipping, and
  the package has been used daily for a month without major issues.

## 13. Explicit non-goals

- Competing with Browserbase, Browser Use, Stagehand, Skyvern. Escarp is a
  runtime layer that could sit below any of them if someone wanted.
- Competing with Strata Identity, Auth0, Okta. Escarp is not an enterprise
  IAM product.
- Being a browser. Lightpanda and Chromium are browsers; Escarp uses them.
- Being an observation layer. DeltaVision is an observation layer. They're
  orthogonal; Escarp integrates with observation layers, it is not one.
- Stealth / anti-bot evasion. Polite identified access is the whole point.
- Solving agent alignment / safety at the model level. Escarp handles the
  security boundary at the *runtime* level only.

## 14. Open questions (answered — preserved for record)

These were raised and answered during the planning review. Preserved here
so the reasoning is auditable.

1. **Lightpanda maturity:** Not mature enough for v0. Python bindings are
   effectively unmaintained. v0 ships Playwright+Chromium only; Lightpanda
   stub exists but raises `NotImplementedError`. Activated in v0.1.

2. **`http-message-signatures` library:** Insufficient. Doesn't handle
   `Signature-Agent` or derived component normalization correctly.
   Implement RFC9421 ourselves with `cryptography` package (~80 lines).

3. **Key directory server bundling:** Ships as `escarp[directory]` optional
   extras. Runs locally for dev on `localhost:8000`; same code deploys to
   davidgao.com. Not a hard dependency for Mode A users.

4. **Profile isolation attack surface:** Cookies, localStorage, IndexedDB,
   Chrome extensions, saved passwords, certificate store. All must be
   isolated. Tests cover each vector. Attack: navigate to
   `accounts.google.com`, read `document.cookie` — expect empty string.

5. **DeltaVision coupling:** Removed from Escarp's dependencies entirely.
   DeltaVision's DOM layer is Playwright-coupled; its CV pipeline is
   backend-agnostic. Escarp exposes `ws.screenshot()` and `ws.page` so
   users can wire in DeltaVision or any other observer themselves.
   Escarp does not observe; it routes.

6. **Router stub design:** `RouteDecision(mode=Mode.AUTONOMOUS, reason="v0:
   router always selects Mode A")`. Stub signature matches final router
   interface exactly. No LLM call in v0 — mode is hardcoded.

## 15. How to work with me on this

- Write plans before you write code. Show me the plan, I push back, then
  you write.
- Tests before features. Security-critical properties get tests first.
- If you hit a decision I haven't anticipated, stop and ask. Don't pick for me.
- If you find a better name for something in the codebase, propose it before
  committing. Names matter.
- Respect the scope boundaries. v0 is Mode A only. Don't write Mode B or
  Lightpanda support "while you're in there." Scope creep is how this doesn't ship.
