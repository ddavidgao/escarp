"""Lease-boundary reset proof.

The reset contract: on lease release, reset the slot -- never close it. This
demo shows the contract holds end-to-end:

    1. Acquire slot 0 via the broker HTTP API.
    2. Drive the leased browser to a real site (YouTube).
    3. Confirm the tab is on YouTube via /json/list.
    4. POST /release.
    5. Confirm the tab snapped back to about:blank.

If step 5 still shows YouTube, the broker is leaking state across leases, which
is the bug David called out.
"""

from __future__ import annotations

import asyncio

import httpx
from playwright.async_api import async_playwright

BROKER = "http://127.0.0.1:7878"


def list_page_tabs(cdp_port: int) -> list[dict[str, str]]:
    r = httpx.get(f"http://127.0.0.1:{cdp_port}/json/list", timeout=5.0)
    r.raise_for_status()
    return [t for t in r.json() if t.get("type") == "page"]


async def main() -> int:
    print("STEP 1  acquire slot 0")
    acq = httpx.post(f"{BROKER}/acquire", json={"holder": "reset-demo", "slot": 0}, timeout=5.0).json()
    token = acq["lease_token"]
    cdp_port = acq["cdp_port"]
    ws_url = acq["cdp_ws_url"]
    print(f"        got lease for cdp_port={cdp_port}, token={token[:12]}...")

    print("\nSTEP 2  drive the leased browser to https://www.youtube.com/")
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(ws_url)
        page = browser.contexts[0].pages[0]
        await page.goto("https://www.youtube.com/", wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(800)
        title = await page.title()
        print(f"        tab title: {title!r}")
        await browser.close()

    print("\nSTEP 3  confirm tab is on YouTube via /json/list")
    for tab in list_page_tabs(cdp_port):
        print(f"        url={tab['url']}  title={tab['title']!r}")

    print("\nSTEP 4  release slot 0")
    rel = httpx.post(f"{BROKER}/release", json={"lease_token": token}, timeout=10.0).json()
    print(f"        slot {rel['slot']} now state={rel['state']!r}")

    print("\nSTEP 5  confirm tab snapped back to about:blank")
    after = list_page_tabs(cdp_port)
    for tab in after:
        print(f"        url={tab['url']}  title={tab['title']!r}")

    on_blank = all(tab["url"] == "about:blank" for tab in after) and len(after) >= 1
    leftover_youtube = any("youtube" in tab["url"].lower() for tab in after)
    print()
    if on_blank and not leftover_youtube:
        print("RESULT  reset-on-release works. No state inherited across the lease boundary.")
        return 0
    print("RESULT  FAIL -- tab did not reset to about:blank after release.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
