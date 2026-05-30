#!/bin/bash
# Empirical CUA focus-behavior validation (post-static-analysis).
#
# Static analysis already settled the targeting model: CUA picks by app, not by
# window. So we are NOT re-litigating title-hint targeting. We are validating
# the OPERATIONAL question:
#
#   "After `escarp focus <slot>`, does Codex CUA reliably act on the newly
#    frontmost CfT key window?"
#
# Four cases (per David's note):
#   1. focus 0, ask CUA to do a visible action -> CUA should act on slot 0.
#   2. focus 1, ask CUA to do a visible action -> CUA should act on slot 1.
#   3. Switch focus repeatedly mid-task -> confirm no drift/stale-window.
#   4. Confirm `osascript activate` + CDP `Page.bringToFront` actually makes
#      the intended WINDOW key, not just the app.
#
# Prerequisites:
#   - escarp daemon running (port 7878)
#   - At least 2 CfT windows in the pool (slot 0 and slot 1)

set -euo pipefail

BROKER="http://127.0.0.1:7878"

acquire() {
    local holder="$1"
    curl -s -X POST "$BROKER/acquire" \
        -H 'content-type: application/json' \
        -d "{\"holder\": \"$holder\"}"
}

label_window() {
    local cdp_port="$1"
    local label="$2"
    python3 - <<PY
import asyncio, sys
sys.path.insert(0, "$HOME/Projects/escarp/.venv/lib/python3.11/site-packages")
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.connect_over_cdp("ws://127.0.0.1:$cdp_port")
        page = b.contexts[0].pages[0]
        await page.goto("https://example.com")
        await page.evaluate("document.title = '$label'")
        await b.close()

asyncio.run(main())
PY
}

focus_slot() {
    local slot="$1"
    local port=$((9222 + slot))
    python3 - <<PY
import asyncio, sys
sys.path.insert(0, "$HOME/Projects/escarp/.venv/lib/python3.11/site-packages")
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.connect_over_cdp("ws://127.0.0.1:$port")
        page = b.contexts[0].pages[0]
        await page.bring_to_front()
        await b.close()

asyncio.run(main())
PY
    osascript -e 'tell application "Google Chrome for Testing" to activate' 2>/dev/null || true
    echo "slot $slot focused (cdp_port $port). Verify on screen + via 'window list' below."
}

window_list() {
    osascript <<'APPLESCRIPT' 2>/dev/null || echo "(osascript window inspection failed; AX permission may be missing)"
tell application "System Events"
    tell process "Google Chrome for Testing"
        set winInfo to {}
        repeat with w in windows
            try
                set end of winInfo to (name of w) & " [frontmost=" & (value of attribute "AXMain" of w as string) & "]"
            end try
        end repeat
        return winInfo
    end tell
end tell
APPLESCRIPT
}

setup() {
    echo "=== acquiring both slots ==="
    A=$(acquire focus-test-A)
    B=$(acquire focus-test-B)
    A_PORT=$(echo "$A" | python3 -c 'import json,sys;print(json.load(sys.stdin)["cdp_port"])')
    B_PORT=$(echo "$B" | python3 -c 'import json,sys;print(json.load(sys.stdin)["cdp_port"])')
    A_TOKEN=$(echo "$A" | python3 -c 'import json,sys;print(json.load(sys.stdin)["lease_token"])')
    B_TOKEN=$(echo "$B" | python3 -c 'import json,sys;print(json.load(sys.stdin)["lease_token"])')
    echo "slot 0 -> cdp $A_PORT  token $A_TOKEN"
    echo "slot 1 -> cdp $B_PORT  token $B_TOKEN"

    echo
    echo "=== labeling windows ==="
    label_window "$A_PORT" "escarp-slot-0"
    label_window "$B_PORT" "escarp-slot-1"

    echo
    echo "=== Case 4 (window-key validation): focus slot 0 ==="
    focus_slot 0
    echo "Current CfT windows (per AX):"
    window_list
    echo
    read -p "Is the window titled 'escarp-slot-0 - Example Domain' the visibly-frontmost one? [y/n] " ok0
    echo

    echo "=== Case 4 (continued): focus slot 1 ==="
    focus_slot 1
    echo "Current CfT windows (per AX):"
    window_list
    echo
    read -p "Is the window titled 'escarp-slot-1 - Example Domain' the visibly-frontmost one? [y/n] " ok1
    echo

    if [ "$ok0" = "y" ] && [ "$ok1" = "y" ]; then
        echo "Case 4 PASS: focus reliably promotes the intended window to key."
    else
        echo "Case 4 FAIL: focus did not produce the expected key window. v1.1 needs"
        echo "a more aggressive focus path (AppleScript per-window indexing, or AX"
        echo "kAXMainWindowAttribute writes). Investigate before shipping."
    fi

    echo
    echo "=== Cases 1, 2, 3: paste these prompts into Codex Desktop CUA ==="
    cat <<'CASES'

(operator just ran: focus 0)
CASE 1 prompt:
  "Navigate this Chrome for Testing window to https://en.wikipedia.org/wiki/Main_Page
   and tell me one headline you see."
  -> EXPECTED: CUA drives slot 0 (escarp-slot-0). Reports a Wikipedia headline.

After Case 1 completes, run:    bash research/cua_test_setup.sh focus 1
Then paste CASE 2 prompt:
  "Navigate this Chrome for Testing window to https://news.ycombinator.com
   and tell me the top story title."
  -> EXPECTED: CUA drives slot 1 (escarp-slot-1).

CASE 3 (drift): start a single Codex CUA prompt while slot 0 is focused.
Mid-prompt, run:                bash research/cua_test_setup.sh focus 1
CASE 3 prompt:
  "Click the search box on this page, type 'computer use', and press enter.
   Then tell me what you see after the page loads."
  -> WATCH FOR: does CUA finish the task on slot 0 (sticky to captured
                windowID), or jump to slot 1 (re-queries frontmost each turn)?
     Either is a data point; record what happened so escarp's `focus` design
     can accommodate.

CASES

    echo
    echo "lease cleanup:"
    echo "  curl -s -X POST $BROKER/release -H 'content-type: application/json' -d '{\"lease_token\":\"$A_TOKEN\"}'"
    echo "  curl -s -X POST $BROKER/release -H 'content-type: application/json' -d '{\"lease_token\":\"$B_TOKEN\"}'"
}

case "${1:-setup}" in
    setup)   setup ;;
    focus)   focus_slot "${2:?slot index required}" ;;
    windows) window_list ;;
    *)       echo "usage: $0 [setup|focus N|windows]"; exit 1 ;;
esac
