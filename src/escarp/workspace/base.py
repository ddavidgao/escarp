"""Browser and BrowserPage protocols — the substrate-agnostic interface."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol, runtime_checkable


@runtime_checkable
class BrowserPage(Protocol):
    """A single page/tab in a browser workspace."""

    async def goto(self, url: str) -> None: ...

    async def click(self, selector: str) -> None: ...

    async def screenshot(self) -> bytes:
        """Return raw PNG bytes."""
        ...

    async def evaluate(self, expression: str) -> object:
        """Evaluate a JavaScript expression and return the result."""
        ...

    async def content(self) -> str:
        """Return the page's full HTML content."""
        ...


@runtime_checkable
class Browser(Protocol):
    """A browser workspace tied to an ephemeral, isolated profile."""

    def new_page(self) -> AbstractAsyncContextManager[BrowserPage]: ...

    async def close(self) -> None: ...
