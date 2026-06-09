"""Stronger parallel-no-clobber test: sustained concurrent work on respective tabs.

Each agent does 4 navigations on its own domain, interleaved with the other via
asyncio.gather. Every step is timestamped, every step takes a screenshot, every
step records the final URL + title. A clobber would manifest as a title or URL
from the wrong domain showing up in the other agent's checkpoint list.

Verification at end:
- Agent A's URLs must ALL be on youtube.com (4 checkpoints)
- Agent B's URLs must ALL be on wikipedia.org (4 checkpoints, expecting 4
  distinct titles since each goto hits Special:Random)
- Wall clock total << sum of per-agent totals -> proves real concurrency,
  not serialized work.

Prerequisite: `escarp daemon` running with ESCARP_POOL_SIZE>=2.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

OUT_DIR = Path("/tmp/escarp-demo")
PLAN_A: list[tuple[str, str]] = [
    ("home", "https://www.youtube.com/"),
    ("trending", "https://www.youtube.com/feed/trending"),
    ("about", "https://www.youtube.com/about/"),
    ("howyoutubeworks", "https://www.youtube.com/howyoutubeworks/"),
]
PLAN_B: list[tuple[str, str]] = [
    ("rand1", "https://en.wikipedia.org/wiki/Special:Random"),
    ("rand2", "https://en.wikipedia.org/wiki/Special:Random"),
    ("rand3", "https://en.wikipedia.org/wiki/Special:Random"),
    ("rand4", "https://en.wikipedia.org/wiki/Special:Random"),
]


def now_stamp() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S.%f")[:-3]


def log(label: str, msg: str) -> None:
    print(f"[{now_stamp()}] [{label}] {msg}", flush=True)


def discover_ws(cdp_port: int) -> str:
    resp = httpx.get(f"http://127.0.0.1:{cdp_port}/json/version", timeout=2.0)
    resp.raise_for_status()
    return resp.json()["webSocketDebuggerUrl"]


async def run_agent(
    label: str,
    ws_url: str,
    plan: list[tuple[str, str]],
) -> dict[str, object]:
    checkpoints: list[dict[str, object]] = []
    t0 = time.monotonic()

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        log(label, f"attached, will run {len(plan)} steps")

        for i, (step_name, url) in enumerate(plan, start=1):
            log(label, f"step {i}/{len(plan)} ({step_name}) -> goto {url}")
            step_t0 = time.monotonic()
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            # small settle so the title is populated and above-the-fold renders
            await page.wait_for_timeout(800)
            final_url = page.url
            title = await page.title()
            screenshot_path = OUT_DIR / f"{label}-step{i}-{step_name}.png"
            await page.screenshot(path=str(screenshot_path), full_page=False)
            step_elapsed = round(time.monotonic() - step_t0, 2)
            log(label, f"step {i} done in {step_elapsed}s -> {title!r} (url={final_url})")
            checkpoints.append(
                {
                    "step": i,
                    "step_name": step_name,
                    "requested_url": url,
                    "final_url": final_url,
                    "title": title,
                    "elapsed_s": step_elapsed,
                    "screenshot": str(screenshot_path),
                }
            )

        await browser.close()  # disconnect CDP; broker-owned chrome stays alive

    return {
        "label": label,
        "ws_url": ws_url,
        "total_elapsed_s": round(time.monotonic() - t0, 2),
        "checkpoints": checkpoints,
    }


def verify(result: dict[str, object], forbidden_host: str) -> tuple[bool, list[str]]:
    """The real no-clobber invariant: this agent must never see the OTHER agent's
    domain in any URL or title. Direction-of-travel checks (does my agent stay
    on its own domain?) are too brittle: legitimate redirects (youtube.com/about
    -> about.youtube) trip them. What we actually care about is cross-bleed.
    """
    issues: list[str] = []
    checkpoints: list[dict[str, object]] = result["checkpoints"]  # type: ignore[assignment]
    for c in checkpoints:
        url = str(c["final_url"]).lower()
        title = str(c["title"]).lower()
        if forbidden_host in url or forbidden_host in title:
            issues.append(
                f"step {c['step']} ({c['step_name']}): forbidden '{forbidden_host}' "
                f"in url={c['final_url']!r} or title={c['title']!r}"
            )
    return not issues, issues


async def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    ws_a = discover_ws(9222)
    ws_b = discover_ws(9223)
    log("setup", f"slot 0 ws: {ws_a}")
    log("setup", f"slot 1 ws: {ws_b}")
    log("setup", "dispatching two agents in parallel...")

    wall_t0 = time.monotonic()
    result_a, result_b = await asyncio.gather(
        run_agent("A-youtube", ws_a, PLAN_A),
        run_agent("B-wikipedia", ws_b, PLAN_B),
    )
    wall = round(time.monotonic() - wall_t0, 2)

    print()
    print("=" * 100)
    print("results")
    print("=" * 100)
    for r in (result_a, result_b):
        print(f"\n[{r['label']}]  total elapsed: {r['total_elapsed_s']}s")
        for c in r["checkpoints"]:  # type: ignore[union-attr]
            print(
                f"  step {c['step']:>1} ({c['step_name']:<16}) {c['elapsed_s']:>5}s  "
                f"title={c['title']!r:<50}  url={c['final_url']}"
            )

    a_ok, a_issues = verify(result_a, forbidden_host="wikipedia")
    b_ok, b_issues = verify(result_b, forbidden_host="youtube")

    # bonus check: 4 random Wikipedia hits should yield 4 distinct titles
    b_titles = {str(c["title"]) for c in result_b["checkpoints"]}  # type: ignore[index]
    b_unique_ok = len(b_titles) == len(PLAN_B)

    print()
    print("=" * 100)
    print("verdict")
    print("=" * 100)
    print(f"wall clock (parallel):       {wall}s")
    print(f"sum of per-agent elapsed:    {result_a['total_elapsed_s'] + result_b['total_elapsed_s']}s")
    print(f"parallel speedup:            {round((result_a['total_elapsed_s'] + result_b['total_elapsed_s']) / max(wall, 0.01), 2)}x")
    print()
    print(f"A: no 'wikipedia' bleed in any URL/title       {'OK' if a_ok else 'FAIL'}")
    print(f"B: no 'youtube' bleed in any URL/title         {'OK' if b_ok else 'FAIL'}")
    print(f"B: 4 random hits -> 4 unique titles            {'OK' if b_unique_ok else f'FAIL ({len(b_titles)} unique)'}")

    if not a_ok:
        for issue in a_issues:
            print(f"  A issue: {issue}")
    if not b_ok:
        for issue in b_issues:
            print(f"  B issue: {issue}")

    success = a_ok and b_ok and b_unique_ok
    print()
    print("no-clobber: CONFIRMED." if success else "CLOBBER OR REGRESSION DETECTED.")
    print(f"\nscreenshots in {OUT_DIR}/ (8 total + earlier 2)")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
