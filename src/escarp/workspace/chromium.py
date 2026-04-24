"""Playwright+Chromium browser backend (v0 substrate)."""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import BrowserContext, async_playwright
from playwright.async_api import Page as PlaywrightPage

from escarp.workspace.base import BrowserPage


class ChromiumPage:
    """Wraps a Playwright Page to satisfy the BrowserPage protocol."""

    def __init__(self, page: PlaywrightPage) -> None:
        self._page = page

    async def goto(self, url: str) -> None:
        await self._page.goto(url)

    async def click(self, selector: str) -> None:
        await self._page.click(selector)

    async def screenshot(self) -> bytes:
        return await self._page.screenshot()

    async def content(self) -> str:
        return await self._page.content()

    async def evaluate(self, expression: str) -> object:
        """Chromium-specific escape hatch. Not part of the Browser protocol."""
        return await self._page.evaluate(expression)

    @property
    def playwright_page(self) -> PlaywrightPage:
        """Escape hatch: raw Playwright Page for advanced users."""
        return self._page


class ChromiumBrowser:
    """Ephemeral Chromium workspace with an isolated profile directory.

    Backed by a launch_persistent_context, which is a BrowserContext (not a
    Browser) in Playwright's type hierarchy. The profile_dir is the isolation
    boundary — it must be a fresh temp dir, never the user's real profile.
    """

    def __init__(self, profile_dir: Path, context: BrowserContext) -> None:
        self._profile_dir = profile_dir
        self._context = context

    @asynccontextmanager
    async def new_page(self) -> AsyncIterator[BrowserPage]:
        page = await self._context.new_page()
        try:
            yield ChromiumPage(page)
        finally:
            await page.close()

    async def close(self) -> None:
        await self._context.close()

    @property
    def profile_dir(self) -> Path:
        return self._profile_dir


@asynccontextmanager
async def launch_chromium(headless: bool = True) -> AsyncIterator[ChromiumBrowser]:
    """Launch an isolated Chromium instance in a fresh temp profile.

    The profile dir is deleted on exit, ensuring no state persists between runs.
    """
    with tempfile.TemporaryDirectory(prefix="escarp-workspace-") as tmp:
        profile_dir = Path(tmp)
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=headless,
                args=["--disable-extensions", "--no-first-run"],
            )
            try:
                yield ChromiumBrowser(profile_dir, context)
            finally:
                await context.close()
