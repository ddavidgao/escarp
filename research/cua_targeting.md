# Codex CUA targeting model — research findings

**Date:** 2026-05-30
**Codex Desktop version:** plugin `computer-use` 1.0.799 (bundle 26.519.41501)
**Method:** Static analysis of `SkyComputerUseClient` binary + plugin manifests +
SKILL.md. Empirical playbook attached below for confirmation.

## Verdict

**Codex CUA targets by APP, not by WINDOW.** Title-based targeting is not in the
addressing model. Within a selected app, CUA operates on the app's "key window"
(frontmost window of that app at turn start).

Three load-bearing strings from the binary:

1. CLI argument help text for app selection:
   > `"App name, full app path, or unambiguous bundle identifier"`

2. Description of CUA's session-start tool:
   > `"Start an app use session if needed, then get the state of the app's key
   > window and return a screenshot and accessibility tree. This must be called
   > once per assistant turn before interacting with the app"`

3. Error path for ambiguity (which exists only at the *app* level, not the
   *window* level):
   > `"Multiple apps share this bundle identifier: ... Use an app name or full
   > app path instead."`

Supporting evidence:
- `ComputerUseIPCFrontmostWindow` + `ComputerUseIPCFrontmostWindowRequest` IPC
  primitives → CUA queries the **frontmost window of the targeted app** at each
  turn start.
- `isFrontmost`, `appDidBecomeActive`, `CGWindow Observer` → CUA watches focus
  changes, not arbitrary windowID selection.
- `windowTitle` exists in the binary but only as a property read for logging /
  reporting back to the model — not as a selector input to any IPC method.
- `appApprovalStore` / `denied_bundle_ids` / `allowed_bundle_ids` → permission
  granularity is per-app, not per-window.

## What this means for escarp v1.1

David's framework from the previous turn applies cleanly:

> "If Codex CUA reliably honors 'act on the Chrome for Testing window titled
> escarp-slot-0,' then v1.1 can be a real coordination layer. If it only
> follows the foreground app/window, then `escarp focus <slot>` becomes the
> main integration primitive."

**Static analysis says: foreground-window-of-app is the only addressable
target.** So `escarp focus <slot>` is the main primitive, not a fallback.

Concrete consequences:

1. **One active CUA-controlled window per app bundle at a time.** Two Codex CUA
   sessions cannot drive two CfT windows concurrently — they'd both resolve to
   "the Chrome for Testing app → its frontmost window." Whoever focused last
   wins. The constraint is per-app-bundle, not global: if separate browser
   apps with distinct bundle IDs were used (e.g. CfT + Chromium nightly +
   Brave), CUA could in principle target each independently. For escarp's
   pool today, all slots share one CfT install and one bundle, so we can
   only safely support **one native-CUA-driven slot at a time**. Concurrent
   multi-slot work uses the CDP path.

2. **Title-setting via CDP is decorative, not load-bearing.** Setting window
   titles to `escarp-slot-0` / `escarp-slot-1` helps the human operator and
   helps CUA's screenshots *describe* the window in narration, but it does NOT
   change which window CUA acts on.

3. **For true concurrent CUA on multiple slots,** we'd need either:
   - **Multiple CfT installs with unique bundle IDs per slot** — heavy, fragile,
     code-signing implications.
   - **Wait for OpenAI** to expose a window-level target in CUA's MCP tool
     schema (file as a feature request).
   - **CDP fallback** — the existing path, which is window-addressable but
     loses the visible-cursor / native-dialog product feel.

4. **`escarp focus <slot>` IS the bridge.** Brings the slot's window to front
   via CDP `Page.bringToFront` + macOS `osascript -e 'tell application "Google
   Chrome for Testing" to activate'` (or `System Events` keystroke
   Cmd-`backtick` walk if multiple windows). After focus, CUA operates on
   the right window.

## Empirical playbook (run this to confirm)

Run the setup script below. It launches two CfT windows in escarp's pool with
distinct page titles, brings slot 0 to front, then waits for the user to paste
test prompts into Codex Desktop's CUA and report what happens.

### Test cases

| # | Setup | Prompt | Predicted outcome (per static analysis) |
|---|-------|--------|---|
| 1 | Two CfT windows titled `escarp-slot-0` / `escarp-slot-1`, both visible. Slot-0 frontmost. | "In the Chrome for Testing window titled `escarp-slot-1`, navigate to example.com" | CUA acts on slot-0 (frontmost), not slot-1. Title hint is narrative-only. |
| 2 | Same setup. Slot-1 frontmost (after running `escarp focus 1`). | Same prompt as #1 | CUA acts on slot-1 (frontmost). Outcome matches the prompt by coincidence — focus is what carried the address, not the title. |
| 3 | Same setup. Slot-0 frontmost. | "Navigate to example.com" (no title hint) | CUA acts on slot-0. |
| 4 | Same setup. Slot-1 frontmost. | "Open the second Chrome for Testing window" | CUA either acts on slot-1 (frontmost = wins) or refuses (ambiguity error). Cannot reliably address "the second window." |
| 5 | Slot-0 frontmost. Trigger something with a native overlay (e.g. download). | "Download the linked file from this page" | CUA can see the download prompt (this is CUA's whole advantage over CDP). |
| 6 | Slot-0 frontmost, then mid-task another app (e.g. Terminal) is clicked. | "Click the X button on the page" | Outcome unclear — does CUA stick to slot-0's windowID it captured, or re-query frontmost and end up clicking in Terminal? This is the "drift" question. |

**To run the experiment yourself, give me the word and I'll set up the two
windows + tell you the exact prompts to paste.**

## Recommendation for v1.1 scope

Given foreground-window-of-app is the only target model:

- **Headline:** "Persistent CfT pool + focus helper + CDP fallback" — v1.1 ships
  what's deliverable today, not what we wish OpenAI had built.
- **`escarp focus <slot>`** — main CUA integration primitive. Brings the slot's
  window forward via CDP `Page.bringToFront` + macOS focus. Sets the page title
  to `escarp-slot-<N>` for human / CUA narration. Idempotent.
- **`escarp setup codex-cua`** — idempotent: detect/install CfT, discover-or-
  launch pool, register `escarp-mcp` with absolute paths. Documents the
  one-CUA-at-a-time constraint clearly.
- **`escarp setup claude-code`** — parity. Same pipeline; Claude Code uses CDP
  via `chrome-devtools-mcp --browser-url` (Claude Code doesn't have native CUA
  yet, so this is the right path for Claude regardless).
- **`escarp demo dual`** — packages the existing lockstep two-holder demo.
  Honest framing: this is the **CDP** path proving two-agent concurrency, not
  the CUA path.
- **README "CUA vs CDP" section** — explicit tradeoff: CUA = better product
  feel (visible cursor, native dialogs, downloads, password prompts) but limited
  to one window at a time. CDP = window-addressable, supports N concurrent
  agents, lower visual fidelity. Use the right one for the task.
- **File OpenAI feature request:** window-level addressability in
  `computer-use` plugin so escarp can offer concurrent CUA agents on a pool.

## Open questions worth answering empirically before v1.1 lands

1. **Drift behavior** (test case #6). If CUA sticks to its captured windowID
   across mid-task focus shifts, `escarp focus` once at lease-time is enough.
   If it re-queries frontmost each turn, escarp needs a focus-watchdog.
2. **`osascript` activate vs CDP bringToFront** — do both work? Which is more
   reliable for CfT on macOS 14+? Settle this before shipping `escarp focus`.
3. **CUA permission grant per CfT install path.** If you reinstall CfT into a
   new path, does the user re-grant Accessibility/Screen Recording? Affects
   `escarp setup` UX.
