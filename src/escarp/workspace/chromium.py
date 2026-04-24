"""Playwright+Chromium browser backend (v0 substrate)."""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import Browser as PlaywrightBrowser
from playwright.async_api import Page as PlaywrightPage
from playwright.async_api import async_playwright

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

    async def evaluate(self, expression: str) -> object:
        return await self._page.evaluate(expression)

    async def content(self) -> str:
        return await self._page.content()

    @property
    def playwright_page(self) -> PlaywrightPage:
        """Escape hatch: raw Playwright Page for advanced users."""
        return self._page


class ChromiumBrowser:
    """Ephemeral Chromium workspace with an isolated profile directory."""

    def __init__(self, profile_dir: Path, _browser: PlaywrightBrowser) -> None:
        self._profile_dir = profile_dir
        self._browser = _browser

    @asynccontextmanager
    def new_page(self) -> AsyncIterator[BrowserPage]:
        async def _inner() -> AsyncIterator[BrowserPage]:
            context = await self._browser.new_context()
            page = await context.new_page()
            try:
                yield ChromiumPage(page)
            finally:
                await context.close()

        return _inner()

    async def close(self) -> None:
        await self._browser.close()

    @property
    def profile_dir(self) -> Path:
        return self._profile_dir


@asynccontextmanager
async def launch_chromium(headless: bool = True) -> AsyncIterator[ChromiumBrowser]:
    """Launch an isolated Chromium instance in a fresh temp profile.

    The profile dir is deleted on exit, ensuring no state persists.
    """
    with tempfile.TemporaryDirectory(prefix="escarp-workspace-") as tmp:
        profile_dir = Path(tmp)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=headless,
                args=["--disable-extensions", "--no-first-run"],
            )
            try:
                yield ChromiumBrowser(profile_dir, browser)  # type: ignore[arg-type]
            finally:
                await browser.close()
