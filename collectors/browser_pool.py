"""Shared Playwright browser pool.

Runs a **single** headless Chromium process and hands out isolated
``BrowserContext`` objects to each collector.  One process with three
contexts uses ~300-500 MB total instead of ~900-1500 MB for three
separate processes – a significant win on memory-constrained WSL
machines.

Usage inside a collector::

    from collectors.browser_pool import BrowserPool

    pool = BrowserPool.shared()
    ctx  = await pool.new_context(user_agent="...", viewport={...})
    # ... use ctx.new_page(), etc.
    await pool.close_context(ctx)          # frees context resources
    # At shutdown the orchestrator calls  pool.shutdown()

The pool is a process-wide singleton (``BrowserPool.shared()``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Superset of Chromium flags used by all three collectors.
_BROWSER_ARGS: list[str] = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-translate",
    "--metrics-recording-only",
    "--no-first-run",
    "--disable-default-apps",
]


class BrowserPool:
    """Manages a single shared Chromium instance.

    * Thread-safe via an :class:`asyncio.Lock`.
    * The underlying browser is lazily launched on the first
      :meth:`new_context` call and re-launched automatically if it
      disconnects.
    * Individual collectors call :meth:`close_context` to tear down
      their own context (freeing pages/cookies) without killing the
      shared process.
    """

    _instance: BrowserPool | None = None

    def __init__(self) -> None:
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._lock = asyncio.Lock()
        self._context_count = 0

    # ── singleton accessor ────────────────────────────────────────
    @classmethod
    def shared(cls) -> BrowserPool:
        """Return the process-wide singleton, creating it if needed."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── internal helpers ──────────────────────────────────────────
    async def _ensure_browser(self) -> None:
        """Launch Chromium if not already running (caller holds lock)."""
        if self._browser is not None and self._browser.is_connected():
            return

        # Tear down stale handles first
        await self._teardown()

        from playwright.async_api import async_playwright

        logger.info("[browser_pool] Starting Playwright …")
        self._playwright = await asyncio.wait_for(
            async_playwright().start(), timeout=30.0
        )

        logger.info("[browser_pool] Launching shared Chromium …")
        self._browser = await asyncio.wait_for(
            self._playwright.chromium.launch(
                headless=True,
                args=_BROWSER_ARGS,
            ),
            timeout=30.0,
        )
        logger.info("[browser_pool] Shared Chromium ready (pid %s)",
                     getattr(self._browser, "process", None))

    async def _teardown(self) -> None:
        """Close browser + playwright without touching the lock."""
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        self._context_count = 0

    # ── public API ────────────────────────────────────────────────
    async def new_context(self, **kwargs: Any) -> Any:
        """Create and return a new isolated ``BrowserContext``.

        All *kwargs* are forwarded to ``browser.new_context()``
        (e.g. ``user_agent``, ``viewport``).
        """
        async with self._lock:
            await self._ensure_browser()
            assert self._browser is not None
            ctx = await self._browser.new_context(**kwargs)
            self._context_count += 1
            logger.debug("[browser_pool] Context created (%d active)",
                         self._context_count)
            return ctx

    async def close_context(self, ctx: Any) -> None:
        """Close a single context (pages inside are closed too)."""
        if ctx is None:
            return
        try:
            await ctx.close()
        except Exception:
            pass
        async with self._lock:
            self._context_count = max(0, self._context_count - 1)
            logger.debug("[browser_pool] Context closed (%d active)",
                         self._context_count)

    async def shutdown(self) -> None:
        """Tear down the browser and Playwright.  Called once at exit."""
        async with self._lock:
            logger.info("[browser_pool] Shutting down shared browser …")
            await self._teardown()
        # Allow a fresh singleton if the process continues (e.g. tests).
        BrowserPool._instance = None

    @property
    def is_connected(self) -> bool:
        return self._browser is not None and self._browser.is_connected()
