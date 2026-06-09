"""Naive parallel-no-clobber test.

Connects to the running broker daemon's two browsers via CDP, drives them in
parallel against two different targets, saves a screenshot from each, and
prints what each side ended up looking at. If the YouTube screenshot is
YouTube and the Wikipedia screenshot is Wikipedia, parallel drives don't
clobber each other -- which is the entire MVP claim.

Prerequisite: `escarp daemon` is running with ESCARP_POOL_SIZE>=2 so slots 0
and 1 are up at cdp_port 9222 and 9223.

Run with: uv run python scripts/demo_two_agents.py
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

OUT_DIR = Path("/tmp/escarp-demo")


def discover_ws(cdp_port: int) -> str:
    resp = httpx.get(f"http://127.0.0.1:{cdp_port}/json/version", timeout=2.0)
    resp.raise_for_status()
    return resp.json()["webSocketDebuggerUrl"]


async def drive(label: str, ws_url: str, target: str) -> dict[str, object]:
    """One 'agent' run: attach over CDP, navigate, screenshot, return facts."""
    async with async_playwright() as pw:
        # connect_over_cdp attaches to an existing browser. close() below
        # disconnects this client but the broker-owned chrome stays alive --
        # exactly the broker's persistence contract.
        browser = await pw.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        t0 = time.monotonic()
        await page.goto(target, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(1500)  # let above-the-fold settle
        elapsed = time.monotonic() - t0

        title = await page.title()
        screenshot_path = OUT_DIR / f"{label}.png"
        await page.screenshot(path=str(screenshot_path), full_page=False)
        await browser.close()

        return {
            "label": label,
            "ws_url": ws_url,
            "target": target,
            "final_url": page.url,
            "title": title,
            "screenshot": str(screenshot_path),
            "elapsed_s": round(elapsed, 2),
        }


async def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    ws_a = discover_ws(9222)
    ws_b = discover_ws(9223)

    print(f"slot 0 cdp ws: {ws_a}")
    print(f"slot 1 cdp ws: {ws_b}\n")
    print("dispatching two agents in parallel...\n")

    results = await asyncio.gather(
        drive("agent-A-youtube", ws_a, "https://www.youtube.com/"),
        drive("agent-B-wikipedia", ws_b, "https://en.wikipedia.org/wiki/Special:Random"),
    )

    print(f"{'agent':<22}{'elapsed':<10}{'title':<60}final_url")
    print("-" * 140)
    for r in results:
        print(
            f"{r['label']:<22}{r['elapsed_s']:<10}{(r['title'] or '')[:58]:<60}{r['final_url']}"
        )

    print()
    print("screenshots:")
    for r in results:
        print(f"  {r['screenshot']}")

    a_ok = "youtube" in (results[0]["final_url"] or "").lower()
    b_ok = "wikipedia" in (results[1]["final_url"] or "").lower()
    if a_ok and b_ok:
        print("\nno-clobber: each agent landed on its own target.")
        return 0
    print("\nCLOBBER DETECTED: at least one agent ended up on the wrong site.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
