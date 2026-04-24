"""Browser workspace abstraction.

The Browser protocol is the seam between Escarp's core and the underlying
browser substrate. v0 uses Chromium via Playwright. v0.1 adds Lightpanda.
Neither leaks into the routing or identity layers.
"""

from __future__ import annotations

from escarp.workspace.base import Browser, BrowserPage
from escarp.workspace.chromium import ChromiumBrowser

__all__ = ["Browser", "BrowserPage", "ChromiumBrowser"]
