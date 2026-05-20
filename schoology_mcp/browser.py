"""Playwright browser lifecycle, session-checked fetching, and keep-alive.

Auto-refresh strategy (Schoology sessions expire quickly):

1. **Detect-and-retry** -- every `fetch()` checks whether the page it got back
   is actually logged in (content-based, not just URL). If not, it re-logs in
   via ClassLink and retries once. This guarantees a fetch never silently
   returns a logged-out page.
2. **Keep-alive** -- a background task periodically re-visits Schoology so the
   sliding session stays warm, so interactive calls rarely pay re-login latency.
3. **Persistence** -- the refreshed session is written back to
   `storage_state.json` so server restarts reuse it.
"""

import asyncio
import logging

from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from . import config
from .auth import login

log = logging.getLogger("schoology_mcp.browser")


class SchoologyClient:
    """Owns one headless browser + context for the MCP server's lifetime."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._context = None
        self._lock = asyncio.Lock()
        self._keepalive_task: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------

    async def _ensure_browser(self) -> None:
        if self._context is not None:
            return
        log.info("Launching Chromium (headless=%s)", config.HEADLESS)
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=config.HEADLESS)
        if config.STORAGE_STATE_PATH.exists():
            log.info("Restoring session from %s", config.STORAGE_STATE_PATH)
            self._context = await self._browser.new_context(
                storage_state=str(config.STORAGE_STATE_PATH)
            )
        else:
            self._context = await self._browser.new_context()

        if config.KEEPALIVE_ENABLED and self._keepalive_task is None:
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            log.info(
                "Keep-alive enabled: refresh every %ds", config.KEEPALIVE_SECONDS
            )

    async def close(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._keepalive_task = None
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()
        self._context = self._browser = self._pw = None

    # -- session management ------------------------------------------------

    async def _login_and_save(self) -> None:
        """Re-run ClassLink login in the live context and persist the session."""
        await login(self._context)
        await self._save_state()
        log.info("Session refreshed and saved to %s", config.STORAGE_STATE_PATH)

    async def _save_state(self) -> None:
        await self._context.storage_state(path=str(config.STORAGE_STATE_PATH))

    async def _keepalive_loop(self) -> None:
        """Periodically refresh the session so it never goes cold."""
        while True:
            try:
                await asyncio.sleep(config.KEEPALIVE_SECONDS)
            except asyncio.CancelledError:
                break
            try:
                async with self._lock:
                    if self._context is None:
                        break
                    await self._refresh_session()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("Keep-alive refresh failed: %s", exc)

    async def _refresh_session(self) -> None:
        """Ping Schoology to keep the session warm; re-login if it has died."""
        url, html = await self._load("/home")
        if _is_logged_in(url, html):
            await self._save_state()  # captures the refreshed cookie expiry
            log.debug("Keep-alive: session healthy")
        else:
            log.info("Keep-alive: session expired -- re-logging in")
            await self._login_and_save()

    # -- fetching ----------------------------------------------------------

    async def fetch(
        self,
        path: str,
        wait_selector: str | None = None,
        extra_wait_ms: int = 2_000,
    ) -> str:
        """Return the rendered HTML of a Schoology page under BASE_URL.

        Serialized with a lock (so concurrent calls and keep-alive share one
        login). Re-logs in and retries once if the page comes back logged out.
        """
        async with self._lock:
            await self._ensure_browser()
            for attempt in (1, 2):
                url, html = await self._load(path, wait_selector, extra_wait_ms)
                if _is_logged_in(url, html):
                    if attempt == 2:
                        await self._save_state()
                    return html
                if attempt == 1:
                    log.info("Session expired while fetching %s -- re-logging in", path)
                    await self._login_and_save()
            raise RuntimeError(
                f"Not logged in after re-login when fetching {path}. "
                "Check credentials or run scripts/login_check.py --show-browser."
            )

    async def _load(
        self,
        path: str,
        wait_selector: str | None = None,
        extra_wait_ms: int = 0,
    ) -> tuple[str, str]:
        """Navigate to a path and return (final_url, html)."""
        page = await self._context.new_page()
        try:
            await page.goto(f"{config.BASE_URL}{path}", wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except PlaywrightTimeout:
                pass
            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=8_000)
                except PlaywrightTimeout:
                    log.warning("Selector %r not found on %s", wait_selector, path)
            if extra_wait_ms:
                await page.wait_for_timeout(extra_wait_ms)
            return page.url, await page.content()
        finally:
            await page.close()


def _is_logged_in(url: str, html: str) -> bool:
    """Content-based login check.

    A logged-in Schoology page stays on schoology.com (not a login URL), has no
    password input, and carries authenticated chrome (`/logout`, site nav).
    """
    if "schoology.com" not in url or "/login" in url:
        return False
    lowered = html.lower()
    if 'type="password"' in lowered or 'name="pass"' in lowered:
        return False
    return "/logout" in lowered or "site-navigation" in lowered
