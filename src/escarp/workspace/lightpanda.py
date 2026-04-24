"""Lightpanda browser backend — v0.1.

Lightpanda (https://github.com/lightpanda-io/browser) is a Zig-based headless
browser, 11x faster and 9x lighter than Chromium. Python CDP bindings are
pre-1.0; this substrate is activated in v0.1 once bindings stabilize.
"""

from __future__ import annotations


def _not_available() -> None:
    raise NotImplementedError(
        "Lightpanda substrate is not available in v0. "
        "Use ChromiumBrowser instead. Lightpanda support arrives in v0.1."
    )


class LightpandaBrowser:
    def __init__(self) -> None:
        _not_available()
