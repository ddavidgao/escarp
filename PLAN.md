# Escarp — Planning Document (v0)

> **For Claude Code:** Read this document top to bottom before writing any code.
> When you've read it, respond with (1) what you think is wrong or underspecified,
> (2) what questions you need answered before starting, and (3) a proposed
> technical design for the v0 MVP. Do not begin implementation until we've
> discussed the design.

## 1. What Escarp is

Escarp is a Python package that routes AI agents to the right mode of web access
for each task — autonomous, delegated, or supervised — automatically, with
identity isolation, session isolation, and observation efficiency built in.

The one-line pitch: **"an identity-aware runtime that picks the right mode of
web access per task, so agents never contaminate the user's personal browser."**

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
- Not a SaaS. No hosted component planned.
- Not a SOC2-compliant production enterprise product. It's personal infra.
- Not a replacement for Browser Use, Stagehand, Skyvern, etc. It sits below them.
- Not a browser extension or a UI product. It's a Python package.

## 5. What Escarp builds on

**Lightpanda** (https://github.com/lightpanda-io/browser) is the fast path
headless browser. Written in Zig, 11× faster and 9× lighter than Chrome,
speaks CDP. Driven via Python through `chrome-devtools-protocol` bindings.
Falls back to Playwright+Chromium when Lightpanda coverage is insufficient.

**DeltaVision** (PyPI, author's existing package) is the observation layer.
Already ships with a 4-layer classifier and 224 tests. Escarp wraps it so
agents get observation-efficiency automatically. Where the browser can emit
DOM mutation events directly, Escarp prefers those and skips the CV pipeline.

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

**v0 must include:**

1. Package scaffolding: `pip install escarp`, clean public API, typed with
   pyright/mypy, tested with pytest.
2. `AgentWorkspace` primitive (Mode A implementation): ephemeral profile dir,
   generated Ed25519 keypair, Lightpanda-first with Playwright+Chromium
   fallback.
3. Web Bot Auth signing: RFC9421 HTTP message signatures on outbound requests.
   Signature-Agent / Signature-Input / Signature headers per the IETF draft.
4. Key directory server: a small FastAPI app that serves JWKS at
   `/.well-known/http-message-signatures-directory`. Intended to run on
   davidgao.com (user hosts their own).
5. Router stub: accepts a task, decides Mode A is the answer (hardcoded for
   v0 since only Mode A exists), dispatches.
6. DeltaVision integration: if the site permits DOM event streaming, emit
   native deltas; otherwise fall back to DeltaVision's CV classifier.
7. Benchmark harness that can produce a three-bar comparison: vanilla
   Playwright, Playwright + DeltaVision, Escarp Mode A.
8. A single end-to-end task that works start to finish as the demo example.

**v0 explicitly does NOT include:**

- Mode B (OAuth). That's v0.1.
- Mode C (supervised session inherit). That's v0.2.
- The real router (Claude picking modes). v0 just hardcodes Mode A.
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
│       ├── __init__.py           # public API: Agent, run, etc.
│       ├── router.py             # task → mode classifier (stubbed in v0)
│       ├── modes/
│       │   ├── __init__.py
│       │   ├── autonomous.py     # Mode A
│       │   ├── delegated.py      # Mode B (stub in v0)
│       │   └── supervised.py     # Mode C (stub in v0)
│       ├── workspace/
│       │   ├── __init__.py
│       │   ├── lightpanda.py     # Lightpanda driver
│       │   ├── chromium.py       # Playwright+Chromium fallback
│       │   └── profile.py        # ephemeral profile mgmt
│       ├── identity/
│       │   ├── __init__.py
│       │   ├── keypair.py        # Ed25519 gen + storage
│       │   ├── signing.py        # RFC9421 message signatures
│       │   └── directory.py      # JWKS key directory server
│       ├── observation/
│       │   ├── __init__.py
│       │   ├── dom_deltas.py     # native DOM mutation stream
│       │   └── deltavision.py    # DeltaVision integration (fallback)
│       ├── credentials/
│       │   ├── __init__.py
│       │   └── keyring.py        # OS keyring wrapper
│       └── config.py
├── tests/
│   ├── test_workspace.py
│   ├── test_signing.py           # verify against crawltest.com
│   ├── test_observation.py
│   └── test_router.py
├── bench/
│   ├── __init__.py
│   ├── harness.py
│   └── tasks/                    # reproducible benchmark tasks
├── examples/
│   ├── apartment_search.py
│   └── hello_escarp.py
└── scripts/
    └── verify_web_bot_auth.py    # tests against crawltest.com
```

## 8. Public API sketch (Claude Code: critique this too)

```python
from escarp import Agent, run

# High-level one-shot
result = run(task="find 5 apartments in West Lafayette under $1500")

# Lower-level for control
agent = Agent(
    name="apartment-hunter",
    # mode=... omitted = router decides; explicit override allowed
)
result = agent.run(task="find 5 apartments in West Lafayette under $1500")

# Even lower-level: direct workspace access for advanced users
with agent.workspace() as ws:
    page = ws.goto("https://example.com")
    page.click("button.search")
    deltas = page.observe()  # yields DeltaVision-format observation events
```

## 9. Stack

- **Language:** Python 3.11+ (match DeltaVision's minimum)
- **Package manager:** `uv` (modern, fast, matches DeltaVision's toolchain)
- **Build:** `hatchling` via pyproject.toml
- **Browser:** Lightpanda (primary), Playwright+Chromium (fallback)
- **HTTP signing:** `http-message-signatures` Python lib + manual implementation
  for the pieces not covered
- **Crypto:** `cryptography` package (Ed25519 via PyNaCl backend)
- **CDP:** raw websockets via `websockets` package, or `chrome-devtools-protocol`
  if it's maintained
- **Credential storage:** `keyring` package
- **Key directory server:** FastAPI + uvicorn, tiny standalone app
- **Testing:** pytest + pytest-asyncio
- **Typing:** mypy strict mode
- **Linting:** ruff
- **LLM:** Anthropic API, `anthropic` SDK, Opus 4.7 as default model
- **Observation:** depends on DeltaVision (`pip install deltavision`)

## 10. Design principles (non-negotiable)

1. **User brings their own keys.** Never bundle API keys. `ANTHROPIC_API_KEY`
   env var, clear documentation.
2. **Nothing phones home.** No telemetry. No analytics. No usage reporting.
3. **Credentials never written to disk in plaintext.** OS keyring only.
4. **Session isolation is load-bearing.** Mode A workspaces must not be able
   to read the user's real Chrome profile. Ever. Test this explicitly.
5. **Fail loud, not silent.** If Lightpanda crashes, crash loudly and say so;
   don't silently fall back and pretend everything's fine. Fallback is opt-in.
6. **Benchmarks have artifacts.** Every benchmark run produces a JSON file
   with token counts, timing, trace ID. Matches DeltaVision's existing pattern.
7. **Tests cover security properties, not just happy paths.** The
   profile-isolation test is more important than the "it navigates to a URL"
   test.
8. **No magic.** The router's decision is observable and explainable. If
   Escarp picks Mode A, it logs why. Users who want to override can.

## 11. Day-by-day v0 build plan

Not a deadline, a sequencing. Claude Code may propose compression or
reorganization.

**Day 1:** Package scaffolding (uv init, pyproject, ruff, mypy, pytest).
`AgentWorkspace` primitive with Playwright+Chromium path (Lightpanda second).
Ephemeral profile directory with isolation test (workspace cannot read main
profile cookies).

**Day 2:** Ed25519 keypair generation. RFC9421 HTTP message signing. Local
signing proxy that intercepts outbound requests from the workspace and adds
signatures.

**Day 3:** Key directory FastAPI app. JWKS serving. Deploy script for
davidgao.com. End-to-end test against crawltest.com/cdn-cgi/web-bot-auth —
expect HTTP 200.

**Day 4:** DeltaVision integration. DOM mutation event streaming where
Lightpanda supports it. Fallback to DeltaVision's CV classifier otherwise.
Reuse DeltaVision's observation format so current users upgrade trivially.

**Day 5:** Router stub. Hardcoded Mode A for v0. Instrumented so when
Modes B and C land later, the router's decision process is already in place.

**Day 6:** Benchmark harness. Three-bar comparison task
(vanilla Playwright vs Playwright+DeltaVision vs Escarp). JSON artifacts.
At least one real end-to-end task that completes successfully.

**Day 7:** README, examples, ship to PyPI as v0.1.0 (not v0.0.1 — there is
a real thing shipped here).

## 12. Longer-term sequence (post-v0)

- **v0.1:** Mode B. OAuth flows, keyring integration, Gmail + Calendar as
  first-class supported services.
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
- Stealth / anti-bot evasion. Polite identified access is the whole point.
- Solving agent alignment / safety at the model level. Escarp handles the
  security boundary at the *runtime* level only.

## 14. Open questions for Claude Code to raise

Do not assume answers to these. Raise them in your first response.

1. Is the Lightpanda Python CDP binding mature enough to rely on, or should
   v0 ship Playwright+Chromium only and add Lightpanda as a follow-up?
2. Is `http-message-signatures` Python library sufficient for our signing
   needs, or do we need to implement RFC9421 ourselves?
3. Should the key directory server be bundled with Escarp (run locally for
   development) or strictly separate (user deploys it themselves)?
4. What's the cleanest way to test profile isolation — are there attack
   vectors beyond cookie reads that need to be covered?
5. Does the DeltaVision observation format assume Playwright-shaped input,
   or is it backend-agnostic? Does that change anything?
6. What does a "router stub" look like in practice — a no-op that always
   returns Mode A, or a real LLM call with Mode B/C paths shortcut to
   "not yet implemented"?

## 15. How to work with me on this

- Write plans before you write code. Show me the plan, I push back, then
  you write.
- When in doubt, match DeltaVision's existing patterns. Consistency across
  the Delta* / Escarp codebase is worth more than local optimization.
- Tests before features. Security-critical properties get tests first.
- If you hit a decision I haven't anticipated, stop and ask. Don't pick for me.
- If you find a better name for something in the codebase, propose it before
  committing. Names matter.
- Respect the scope boundaries. v0 is Mode A. Don't write Mode B "while you're
  in there." Scope creep is how this doesn't ship.
