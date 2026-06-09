"""Two holders, two leases, two browsers, driven in lockstep concurrency.

Unlike the smart-agent subagent demos (which prove reasoning + pool-state
visibility), this script proves the visible-on-screen concurrency claim: both
browser windows do work at the same wall-clock instants, not your-then-mine.

Mechanism: acquire both leases up front, then asyncio.gather two Playwright
drive coroutines that bring_to_front and step through navigate/scroll/click in
parallel. Two coroutines on one event loop genuinely interleave at every
`await` -- you'll see both windows pop forward, both navigations start, both
scrolls happen at the same time.

Prereqs:
    escarp launch-pool        # if chromes not already up
    escarp daemon &           # broker on 7878
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

BROKER = "http://127.0.0.1:7878"
OUT_DIR = Path("/tmp/escarp-concurrent")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(who: str, msg: str) -> None:
    print(f"[{ts()}] [{who}] {msg}", flush=True)


def acquire(holder: str) -> dict:
    r = httpx.post(f"{BROKER}/acquire", json={"holder": holder}, timeout=5.0)
    r.raise_for_status()
    return r.json()


def release(lease_token: str) -> None:
    httpx.post(f"{BROKER}/release", json={"lease_token": lease_token}, timeout=10.0)


async def drive_holder(
    who: str,
    ws_url: str,
    plan: list[tuple[str, str]],
    start_barrier: asyncio.Event,
) -> dict:
    """Connect to leased browser, wait at barrier, then step through plan
    in lockstep with the other holder."""
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(ws_url)
        page = browser.contexts[0].pages[0]
        await page.bring_to_front()
        log(who, "connected + bring_to_front; waiting at start barrier")

        await start_barrier.wait()
        log(who, "barrier crossed -- driving begins")

        checkpoints: list[dict] = []
        for step_i, (label, action) in enumerate(plan, start=1):
            log(who, f"step {step_i} ({label}) -> {action[:60]}")
            t0 = time.monotonic()

            if action.startswith("goto:"):
                url = action[len("goto:"):]
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            elif action.startswith("scroll:"):
                px = int(action[len("scroll:"):])
                await page.evaluate(f"window.scrollBy({{top: {px}, behavior: 'smooth'}})")
            elif action.startswith("click:"):
                selector = action[len("click:"):]
                await page.locator(selector).first.click()
                await page.wait_for_load_state("domcontentloaded", timeout=20000)
            else:
                raise ValueError(f"unknown action: {action}")

            # All actions get a 5s settle so they're visible on screen.
            await page.wait_for_timeout(5000)

            title = await page.title()
            elapsed = round(time.monotonic() - t0, 2)
            log(who, f"step {step_i} done in {elapsed}s -> {title!r}")
            checkpoints.append(
                {
                    "step": step_i,
                    "label": label,
                    "action": action,
                    "title": title,
                    "url": page.url,
                    "elapsed_s": elapsed,
                }
            )

        screenshot_path = OUT_DIR / f"{who}.png"
        await page.screenshot(path=str(screenshot_path), full_page=False)
        log(who, f"final screenshot -> {screenshot_path}")

        await browser.close()  # disconnects this CDP client; broker-owned chrome stays alive
        return {"who": who, "checkpoints": checkpoints, "screenshot": str(screenshot_path)}


PLAN_A: list[tuple[str, str]] = [
    ("hn home",         "goto:https://news.ycombinator.com"),
    ("scroll down",     "scroll:800"),
    ("click comments",  "click:a:has-text('comments')"),
    ("scroll comments", "scroll:600"),
]
PLAN_B: list[tuple[str, str]] = [
    ("wiki random",     "goto:https://en.wikipedia.org/wiki/Special:Random"),
    ("scroll article",  "scroll:700"),
    ("click first link", "click:#mw-content-text a[href^='/wiki/']:not([href*=':'])"),
    ("scroll new",      "scroll:500"),
]


async def main() -> int:
    log("orchestrator", "acquiring two leases up front")
    lease_a = acquire("holder-A")
    lease_b = acquire("holder-B")
    log("orchestrator", f"A got slot {lease_a['slot']} on cdp_port {lease_a['cdp_port']}")
    log("orchestrator", f"B got slot {lease_b['slot']} on cdp_port {lease_b['cdp_port']}")

    barrier = asyncio.Event()

    # Pre-connect (bring windows forward, attach playwright) before barrier so
    # actual drive work starts in lockstep, not "whoever connects first wins."
    holder_a = drive_holder("A-hn",   lease_a["cdp_ws_url"], PLAN_A, barrier)
    holder_b = drive_holder("B-wiki", lease_b["cdp_ws_url"], PLAN_B, barrier)

    # Let both coroutines reach the barrier wait. asyncio.gather starts them
    # cooperatively; small sleep ensures both have called bring_to_front and
    # parked on the barrier before we release it.
    task_a = asyncio.create_task(holder_a)
    task_b = asyncio.create_task(holder_b)
    await asyncio.sleep(1.0)
    log("orchestrator", "RELEASING BARRIER -- both holders fire step 1 simultaneously")
    barrier.set()

    wall_t0 = time.monotonic()
    results = await asyncio.gather(task_a, task_b)
    wall_elapsed = round(time.monotonic() - wall_t0, 2)

    log("orchestrator", f"both holders done. wall-clock drive time: {wall_elapsed}s")
    log("orchestrator", "releasing leases")
    release(lease_a["lease_token"])
    release(lease_b["lease_token"])
    log("orchestrator", "both released; broker reset both tabs to about:blank")

    print()
    print("=" * 100)
    print("summary")
    print("=" * 100)
    for r in results:
        print(f"\n[{r['who']}]")
        for cp in r["checkpoints"]:
            print(f"  step {cp['step']} ({cp['label']:<18}) {cp['elapsed_s']:>5}s  {cp['title']!r}")
        print(f"  screenshot: {r['screenshot']}")
    print(f"\nwall-clock drive: {wall_elapsed}s  (sum of per-holder = {sum(sum(c['elapsed_s'] for c in r['checkpoints']) for r in results):.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
