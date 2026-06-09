"""Chrome for Testing launcher and CDP discovery.

Phase 2 minimal: detached subprocess + HTTP /json/version discovery. No lease
state, no reset logic — that's Phase 3. This module owns "how do we make a
CfT window exist on the user's monitor and surface its CDP websocket URL."

Discovery contract (per Phase 0 smoke test):
- Primary: GET http://127.0.0.1:<cdp_port>/json/version -> webSocketDebuggerUrl.
  Empirically confirmed working in CfT 149.0.7827.54 on macOS arm64.
- The DevToolsActivePort file the original plan called for does NOT appear in
  macOS profile dirs in our testing.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

# Locked launch flags.
_BASE_FLAGS: tuple[str, ...] = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-extensions",
    # macOS: per-slot CUA app bundles have unique bundle IDs. Without this,
    # Chromium asks for "Chromium Safe Storage" keychain access per slot.
    "--use-mock-keychain",
)


class BrowserLaunchError(RuntimeError):
    pass


@dataclass
class ManagedBrowser:
    slot: int
    cdp_port: int
    cdp_ws_url: str
    profile_dir: Path
    process: subprocess.Popen[bytes]

    @property
    def pid(self) -> int:
        return self.process.pid

    def is_alive(self) -> bool:
        return self.process.poll() is None


def launch_cft(
    *,
    slot: int,
    binary: Path,
    profile_dir: Path,
    cdp_port: int,
    initial_url: str = "about:blank",
    discovery_timeout: float = 10.0,
) -> ManagedBrowser:
    """Launch a detached Chrome for Testing process and resolve its CDP URL.

    The process is started in a new session (`start_new_session=True`) so that
    when our parent shell or the daemon exits, the browser is reparented to
    launchd/init rather than being killed. This is the load-bearing detail
    for the "browsers outlive every agent" persistence contract.
    """
    if not binary.exists():
        raise BrowserLaunchError(f"Chrome for Testing binary not found at {binary}")

    profile_dir.mkdir(parents=True, exist_ok=True)

    args = [
        str(binary),
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={profile_dir}",
        *_BASE_FLAGS,
        initial_url,
    ]

    process = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )

    try:
        ws_url = _resolve_ws_url(cdp_port, timeout=discovery_timeout)
    except Exception:
        # Discovery failed; don't leak the process.
        _terminate(process)
        raise

    return ManagedBrowser(
        slot=slot,
        cdp_port=cdp_port,
        cdp_ws_url=ws_url,
        profile_dir=profile_dir,
        process=process,
    )


def _resolve_ws_url(cdp_port: int, *, timeout: float) -> str:
    """Poll /json/version until we get a webSocketDebuggerUrl or time out."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(
                f"http://127.0.0.1:{cdp_port}/json/version",
                timeout=1.0,
            )
            resp.raise_for_status()
            ws_url: str | None = resp.json().get("webSocketDebuggerUrl")
            if ws_url:
                return ws_url
        except Exception as exc:
            last_error = exc
        time.sleep(0.1)
    raise BrowserLaunchError(
        f"CDP discovery timed out after {timeout}s on port {cdp_port}"
        + (f" (last error: {last_error})" if last_error else "")
    )


def _terminate(process: subprocess.Popen[bytes], *, grace: float = 3.0) -> None:
    if process.poll() is not None:
        return
    try:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=grace)


def shutdown(browser: ManagedBrowser, *, grace: float = 3.0) -> None:
    _terminate(browser.process, grace=grace)


async def reset_browser_state(cdp_port: int, *, target_url: str = "about:blank") -> int:
    """Reset a leased browser to a clean slate.

    The contract is "on lease release, reset -- never close." Closes
    every page-type tab except a freshly-created one navigated to target_url.
    Internal target types (browser_ui, service_worker, etc.) are left alone --
    those are chrome's own plumbing, not user-visible tabs.

    Uses only the CfT /json HTTP endpoints (verified working in 149) -- no
    websocket dance, no extra deps beyond httpx.

    Returns the number of stale tabs closed (zero on a pristine browser).
    """
    import httpx  # local import keeps the sync launch_cft path light

    async with httpx.AsyncClient(timeout=5.0) as client:
        # Order matters: create the new clean tab FIRST so we never close down
        # to zero tabs (which would close the only window and kill the process).
        new_resp = await client.put(f"http://127.0.0.1:{cdp_port}/json/new?{target_url}")
        new_resp.raise_for_status()
        new_id = new_resp.json()["id"]

        list_resp = await client.get(f"http://127.0.0.1:{cdp_port}/json/list")
        list_resp.raise_for_status()
        tabs = list_resp.json()

        closed = 0
        for tab in tabs:
            if tab.get("type") != "page" or tab.get("id") == new_id:
                continue
            try:
                close_resp = await client.get(
                    f"http://127.0.0.1:{cdp_port}/json/close/{tab['id']}"
                )
                if close_resp.status_code < 400:
                    closed += 1
            except httpx.HTTPError:
                # Best-effort: a tab that vanished mid-loop isn't a problem.
                pass
        return closed


def find_cft_binary() -> Path | None:
    """Best-effort discovery for the Chrome for Testing binary.

    Order: $ESCARP_CFT_BINARY env, ~/.escarp/chrome/**, ./chrome/** (repo-local
    dev install). Returns None if nothing usable is found.
    """
    env = os.environ.get("ESCARP_CFT_BINARY")
    if env:
        path = Path(env)
        if path.exists():
            return path
    candidates = [
        Path.home() / ".escarp" / "chrome",
        Path.cwd() / "chrome",
    ]
    for root in candidates:
        if not root.exists():
            continue
        # @puppeteer/browsers layout: chrome/<platform>-<ver>/chrome-<plat>/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing
        for app in root.rglob("Google Chrome for Testing.app"):
            macos = app / "Contents" / "MacOS" / "Google Chrome for Testing"
            if macos.exists():
                return macos
        # Linux layout: chrome/<platform>-<ver>/chrome-linux64/chrome
        for binary in root.rglob("chrome"):
            if binary.is_file() and os.access(binary, os.X_OK) and "chrome-" in str(binary.parent):
                return binary
    # Last resort: PATH lookup
    which = shutil.which("chrome-for-testing")
    return Path(which) if which else None
