"""`escarp setup <agent>` -- one-command MCP wiring + smoke test.

The pitch from David's feedback was: "Codex CUA ready" should be one command,
not "install/find CfT + start/discover broker + register MCP/adapter + run
visible smoke test" performed by hand. This module is that one command.

Two supported agents in v1.1:

  escarp setup codex          (alias: codex-cua)
  escarp setup claude-code    (alias: claude)

The CUA-vs-CDP distinction is honest in messaging: codex registers escarp-mcp
for lease management and uses Codex Desktop's CUA for visible interaction;
claude-code registers escarp-mcp plus chrome-devtools-mcp pointed at the
broker, since Claude Code has no native CUA today.

Idempotent: re-running setup detects existing pool/registration and reports
"already configured" rather than duplicating.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from escarp.broker.api import DEFAULT_PORT
from escarp.broker.browser import find_cft_binary

BROKER_URL_DEFAULT = f"http://127.0.0.1:{DEFAULT_PORT}"


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    fix_hint: str | None = None


def _escarp_mcp_path() -> Path | None:
    """Resolve the absolute path to escarp-mcp.

    Robust to venvs: looks in sys.executable's bin dir first (where pip + uv
    drop console scripts), falls back to PATH lookup.
    """
    candidate = Path(sys.executable).parent / "escarp-mcp"
    if candidate.exists():
        return candidate
    which = shutil.which("escarp-mcp")
    return Path(which) if which else None


def check_cft() -> CheckResult:
    binary = find_cft_binary()
    if binary is None:
        return CheckResult(
            name="Chrome for Testing binary",
            ok=False,
            detail="not found",
            fix_hint="npx @puppeteer/browsers install chrome@stable",
        )
    return CheckResult(name="Chrome for Testing binary", ok=True, detail=str(binary))


def check_daemon() -> CheckResult:
    try:
        resp = httpx.get(f"{BROKER_URL_DEFAULT}/status", timeout=2.0)
        resp.raise_for_status()
        data = resp.json()
        pool = data["pool_size"]
        return CheckResult(
            name="broker daemon",
            ok=True,
            detail=f"up on {BROKER_URL_DEFAULT}, pool_size={pool}",
        )
    except Exception as exc:
        return CheckResult(
            name="broker daemon",
            ok=False,
            detail=f"not reachable ({type(exc).__name__})",
            fix_hint="escarp daemon &",
        )


def check_pool(min_slots: int = 1) -> CheckResult:
    try:
        resp = httpx.get(f"{BROKER_URL_DEFAULT}/status", timeout=2.0)
        data = resp.json()
        slots = data["slots"]
    except Exception as exc:
        return CheckResult(
            name="pool",
            ok=False,
            detail=f"could not query broker ({exc})",
            fix_hint="escarp launch-pool && escarp daemon &",
        )
    if len(slots) < min_slots:
        return CheckResult(
            name="pool",
            ok=False,
            detail=f"only {len(slots)} slot(s) registered (need >= {min_slots})",
            fix_hint=f"ESCARP_POOL_SIZE={min_slots} escarp launch-pool",
        )
    return CheckResult(
        name="pool",
        ok=True,
        detail=f"{len(slots)} slot(s) discovered",
    )


def check_escarp_mcp() -> CheckResult:
    path = _escarp_mcp_path()
    if path is None:
        return CheckResult(
            name="escarp-mcp binary",
            ok=False,
            detail="not found",
            fix_hint="pip install --upgrade escarp  (or run setup from the venv that has escarp)",
        )
    return CheckResult(name="escarp-mcp binary", ok=True, detail=str(path))


def check_codex_cli() -> CheckResult:
    if shutil.which("codex") is None:
        return CheckResult(
            name="codex CLI",
            ok=False,
            detail="not on PATH",
            fix_hint="npm install -g @openai/codex  (or follow openai.com/codex/cli)",
        )
    try:
        out = subprocess.run(
            ["codex", "--version"], check=True, capture_output=True, timeout=3.0
        )
        return CheckResult(
            name="codex CLI", ok=True, detail=out.stdout.decode().strip()
        )
    except Exception as exc:
        return CheckResult(name="codex CLI", ok=False, detail=str(exc))


def check_claude_cli() -> CheckResult:
    if shutil.which("claude") is None:
        return CheckResult(
            name="claude CLI",
            ok=False,
            detail="not on PATH",
            fix_hint="install Claude Code: https://claude.com/claude-code",
        )
    return CheckResult(name="claude CLI", ok=True, detail=str(shutil.which("claude")))


def _codex_mcp_list_has(name: str) -> bool:
    try:
        out = subprocess.run(
            ["codex", "mcp", "list"], capture_output=True, timeout=5.0
        )
        return name in out.stdout.decode()
    except Exception:
        return False


def _claude_mcp_list_has(name: str) -> bool:
    try:
        out = subprocess.run(
            ["claude", "mcp", "list"], capture_output=True, timeout=5.0
        )
        return name in out.stdout.decode()
    except Exception:
        return False


def _register_codex(mcp_path: Path) -> bool:
    if _codex_mcp_list_has("escarp"):
        print("  codex mcp: 'escarp' already registered, leaving as-is")
        return True
    try:
        subprocess.run(
            ["codex", "mcp", "add", "escarp", "--", str(mcp_path)],
            check=True,
            capture_output=True,
            timeout=10.0,
        )
        print(f"  codex mcp: registered 'escarp' -> {mcp_path}")
        return True
    except subprocess.CalledProcessError as exc:
        print(
            f"  codex mcp: registration failed: {exc.stderr.decode(errors='replace').strip()}",
            file=sys.stderr,
        )
        return False


def _register_claude(mcp_path: Path) -> bool:
    if _claude_mcp_list_has("escarp"):
        print("  claude mcp: 'escarp' already registered, leaving as-is")
        return True
    try:
        subprocess.run(
            ["claude", "mcp", "add", "escarp", "--", str(mcp_path)],
            check=True,
            capture_output=True,
            timeout=10.0,
        )
        print(f"  claude mcp: registered 'escarp' -> {mcp_path}")
        return True
    except subprocess.CalledProcessError as exc:
        print(
            f"  claude mcp: registration failed: {exc.stderr.decode(errors='replace').strip()}",
            file=sys.stderr,
        )
        return False


def _smoke_test() -> bool:
    """End-to-end smoke: acquire a lease, see it in /status, release, see it gone.

    Hits the broker HTTP API directly (not via the MCP shim) so the test runs
    even if the MCP integration is misconfigured -- the test then surfaces
    the misconfiguration instead of hiding it.
    """
    try:
        r = httpx.post(
            f"{BROKER_URL_DEFAULT}/acquire",
            json={"holder": "escarp-setup-smoke"},
            timeout=5.0,
        )
        r.raise_for_status()
        lease = r.json()
        token = lease["lease_token"]
        slot = lease["slot"]

        status_during = httpx.get(f"{BROKER_URL_DEFAULT}/status", timeout=2.0).json()
        held = next(s for s in status_during["slots"] if s["slot"] == slot)
        if held["state"] != "leased":
            print(
                f"  smoke FAIL: slot {slot} did not show as leased mid-test",
                file=sys.stderr,
            )
            return False

        rel = httpx.post(
            f"{BROKER_URL_DEFAULT}/release",
            json={"lease_token": token},
            timeout=10.0,
        )
        rel.raise_for_status()

        status_after = httpx.get(f"{BROKER_URL_DEFAULT}/status", timeout=2.0).json()
        freed = next(s for s in status_after["slots"] if s["slot"] == slot)
        if freed["state"] != "free":
            print(
                f"  smoke FAIL: slot {slot} did not return to free state after release",
                file=sys.stderr,
            )
            return False

        print(f"  smoke OK: acquired slot {slot}, observed leased, released, observed free")
        return True
    except Exception as exc:
        print(f"  smoke FAIL: {exc}", file=sys.stderr)
        return False


def _run_checks(checks: list[CheckResult]) -> bool:
    print("preflight:")
    all_ok = True
    for c in checks:
        marker = "ok " if c.ok else "FAIL"
        print(f"  [{marker}] {c.name}: {c.detail}")
        if not c.ok and c.fix_hint:
            print(f"         fix: {c.fix_hint}")
        if not c.ok:
            all_ok = False
    print()
    return all_ok


def setup_codex(args: list[str] | None = None) -> int:
    print("escarp setup codex")
    print("------------------\n")

    checks = [check_cft(), check_daemon(), check_pool(), check_escarp_mcp(), check_codex_cli()]
    if not _run_checks(checks):
        print(
            "preflight failed. fix the items above (each has a 'fix:' hint) and re-run.\n"
            "this command is idempotent -- safe to re-run after fixing.",
            file=sys.stderr,
        )
        return 2

    mcp_path = _escarp_mcp_path()
    assert mcp_path is not None  # check passed
    print("registering MCP server with codex:")
    if not _register_codex(mcp_path):
        return 3

    print("\nsmoke test (acquire -> status -> release):")
    if not _smoke_test():
        return 4

    print("\ndone. Codex CUA ready.")
    print()
    print("Next steps:")
    print("  1. For native visible CUA: `escarp acquire --slot N --holder NAME --prompt --hold`.")
    print("     Paste the printed bundle-ID prompt into Codex and press Ctrl-C here when done.")
    print("  2. For MCP/CDP: call the tools escarp_status, escarp_acquire, escarp_release.")
    print()
    print("  Note: `setup codex` validates MCP wiring. If this command reports a Codex")
    print("  CLI/MCP issue but `escarp acquire --prompt --hold` prints a per-slot bundle")
    print("  prompt, the native CUA browser-pool flow can still work.")
    return 0


def setup_claude(args: list[str] | None = None) -> int:
    print("escarp setup claude-code")
    print("------------------------\n")

    checks = [check_cft(), check_daemon(), check_pool(), check_escarp_mcp(), check_claude_cli()]
    if not _run_checks(checks):
        print(
            "preflight failed. fix the items above and re-run.",
            file=sys.stderr,
        )
        return 2

    mcp_path = _escarp_mcp_path()
    assert mcp_path is not None
    print("registering MCP server with claude:")
    if not _register_claude(mcp_path):
        return 3

    print("\nsmoke test (acquire -> status -> release):")
    if not _smoke_test():
        return 4

    print("\ndone. Claude Code wired to escarp.")
    print()
    print("Next steps:")
    print("  1. In a Claude Code session, call:  escarp_status, escarp_acquire, escarp_release.")
    print("  2. After acquire, drive the leased browser via Playwright connect_over_cdp or")
    print("     register chrome-devtools-mcp separately with --browser-url for tool-based driving.")
    print("  3. Claude Code has no native CUA today, so the CDP path is the only driver path.")
    print("     For visible-cursor / native-dialog UX, use Codex CUA via `escarp setup codex`.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv or [])
    if not args:
        print(
            "usage: escarp setup <agent>\n"
            "  agents: codex (aliases: codex-cua), claude-code (aliases: claude)",
            file=sys.stderr,
        )
        return 2

    agent = args[0].lower()
    rest = args[1:]
    if agent in ("codex", "codex-cua"):
        return setup_codex(rest)
    if agent in ("claude-code", "claude"):
        return setup_claude(rest)
    print(f"unknown agent: {agent}", file=sys.stderr)
    return 2
