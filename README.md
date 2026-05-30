# escarp

Identity-aware runtime for parallel coding agents. A single broker daemon owns a pool of persistent Chrome for Testing windows and hands CDP endpoint leases to coding agents (Claude Code, Codex) over MCP — so N worktrees can run N agents with N isolated browsers and no stale-lock hell.

> **Status:** v2 in progress on the [`v2` branch](https://github.com/ddavidgao/escarp/tree/v2). v0 (`pip install escarp==0.1.0`) is the previous Playwright-per-task shape and is being replaced. Design in [V2_PLAN.md](V2_PLAN.md).

## v2 quick start (development, from `v2` branch)

```bash
# install Chrome for Testing into the repo (gitignored)
npx @puppeteer/browsers install chrome@stable

# bring up a pool of 2 persistent browsers
ESCARP_POOL_SIZE=2 \
ESCARP_CFT_BINARY="$PWD/chrome/mac_arm-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing" \
uv run escarp daemon
```

In another shell, drive both browsers in parallel against different sites and verify no cross-bleed:

```bash
uv run python scripts/demo_concurrent_tabs.py
```

Two agents, 4 navigations each, ~2x parallel speedup, 8 screenshots written to `/tmp/escarp-demo/`.

## License

MIT
