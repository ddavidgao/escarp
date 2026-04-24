"""Profile isolation tests — the security-critical property.

An Escarp workspace must never be able to read credentials, cookies, or
storage from the user's real browser profile. These tests verify that
boundary using real Chromium launches, not mocks.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from escarp.workspace.chromium import launch_chromium


@pytest.mark.asyncio
async def test_workspace_cannot_read_cookies_from_separate_profile() -> None:
    """A workspace launched at path A cannot read cookies written into path B.

    This is the core isolation guarantee: even if an attacker somehow points
    a workspace at a domain where the user has real cookies, the workspace
    profile is fresh and contains nothing.
    """
    cookie_name = "escarp_isolation_sentinel"
    cookie_value = "should_not_be_visible"

    # Write a known cookie into a "real profile" dir via a separate Chromium context.
    with tempfile.TemporaryDirectory(prefix="escarp-real-profile-") as real_dir:
        async with async_playwright() as pw:
            real_ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=real_dir,
                headless=True,
            )
            await real_ctx.add_cookies([{
                "name": cookie_name,
                "value": cookie_value,
                "domain": "example.com",
                "path": "/",
            }])
            await real_ctx.close()

        # Launch an Escarp workspace — different temp dir, knows nothing of real_dir.
        async with launch_chromium(headless=True) as ws:
            # Confirm the workspace profile dir is not the real profile dir.
            assert ws.profile_dir != Path(real_dir)

            async with ws.new_page() as page:
                await page.goto("https://example.com")
                cookies = await page.evaluate("document.cookie")
                assert cookie_name not in str(cookies), (
                    f"Workspace read a cookie ({cookie_name}) from a separate profile. "
                    "Isolation is broken."
                )


@pytest.mark.asyncio
async def test_workspace_profile_dir_is_not_home_chrome_profile() -> None:
    """The workspace profile dir must not be the user's real Chrome profile.

    Chromium defaults to ~/.config/google-chrome on Linux,
    ~/Library/Application Support/Google/Chrome on macOS,
    %LOCALAPPDATA%\\Google\\Chrome\\User Data on Windows.
    None of these should ever be the workspace dir.
    """
    import platform
    from pathlib import Path

    if platform.system() == "Darwin":
        real_profile = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    elif platform.system() == "Windows":
        import os
        real_profile = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    else:
        real_profile = Path.home() / ".config" / "google-chrome"

    async with launch_chromium(headless=True) as ws:
        assert ws.profile_dir != real_profile
        # Also verify it's not a subdirectory of the real profile.
        assert not ws.profile_dir.is_relative_to(real_profile), (
            f"Workspace profile {ws.profile_dir} is inside the real Chrome profile {real_profile}. "
            "Isolation is broken."
        )


@pytest.mark.asyncio
async def test_workspace_storage_is_empty_on_launch() -> None:
    """A fresh workspace has no localStorage entries for any domain."""
    async with launch_chromium(headless=True) as ws, ws.new_page() as page:
        await page.goto("https://example.com")
        storage_len = await page.evaluate("localStorage.length")
        assert storage_len == 0, (
            f"Fresh workspace has {storage_len} localStorage entries. "
            "Expected 0 — workspace is not clean."
        )


@pytest.mark.asyncio
async def test_workspace_cookies_are_empty_on_launch() -> None:
    """A fresh workspace has no cookies for any domain."""
    async with launch_chromium(headless=True) as ws, ws.new_page() as page:
        await page.goto("https://example.com")
        cookies = await page.evaluate("document.cookie")
        assert cookies == "", (
            f"Fresh workspace has cookies on launch: {cookies!r}. "
            "Expected empty string."
        )


@pytest.mark.asyncio
async def test_workspace_profile_dir_is_cleaned_up_after_exit() -> None:
    """The temp profile dir is deleted when the workspace context exits."""
    captured: list[Path] = []

    async with launch_chromium(headless=True) as ws:
        captured.append(ws.profile_dir)
        assert ws.profile_dir.exists()

    assert not captured[0].exists(), (
        f"Profile dir {captured[0]} still exists after workspace exit. "
        "Temp dir was not cleaned up."
    )


@pytest.mark.asyncio
async def test_two_workspaces_have_separate_profile_dirs() -> None:
    """Concurrent workspaces each get a distinct, isolated profile directory."""
    dirs: list[Path] = []

    async with launch_chromium(headless=True) as ws1, launch_chromium(headless=True) as ws2:
        dirs.append(ws1.profile_dir)
        dirs.append(ws2.profile_dir)

    assert dirs[0] != dirs[1], "Two workspaces share the same profile dir. Not isolated."


@pytest.mark.asyncio
async def test_chromium_browser_satisfies_browser_protocol() -> None:
    """ChromiumBrowser structurally satisfies the Browser protocol at runtime."""
    from escarp.workspace.base import Browser

    async with launch_chromium(headless=True) as ws:
        assert isinstance(ws, Browser), (
            "ChromiumBrowser does not satisfy the Browser protocol. "
            "Check base.py for interface drift."
        )
