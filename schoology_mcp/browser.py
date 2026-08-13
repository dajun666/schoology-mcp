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
import os
import re
import time
from datetime import datetime

from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from . import auth, config
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
        self._context = None
        if config.STORAGE_STATE_PATH.exists():
            log.info("Restoring session from %s", config.STORAGE_STATE_PATH)
            try:
                self._context = await self._browser.new_context(
                    storage_state=str(config.STORAGE_STATE_PATH)
                )
            except Exception as exc:  # noqa: BLE001 - truncated/corrupt state file
                # A half-written session file must not make the server
                # unstartable; a fresh context just costs one extra login.
                log.warning(
                    "Ignoring unreadable %s (%s) -- starting a fresh session",
                    config.STORAGE_STATE_PATH, exc,
                )
        if self._context is None:
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
        """Persist cookies atomically.

        An interactive session and a scheduled run will overlap sooner or later;
        a torn write leaves an unparseable storage_state.json that bricks the
        next startup. Write to a sibling temp file, then rename (atomic on the
        same filesystem).
        """
        target = config.STORAGE_STATE_PATH
        tmp = target.with_suffix(target.suffix + ".tmp")
        await self._context.storage_state(path=str(tmp))
        os.replace(tmp, target)

    async def session_info(self) -> dict:
        """Cookie lifetime and session freshness, for get_health()."""
        info: dict = {
            "storage_state_path": str(config.STORAGE_STATE_PATH),
            "storage_state_mtime": None,
            "cookie_expiry_epoch": None,
            "seconds_to_expiry": None,
        }
        if config.STORAGE_STATE_PATH.exists():
            info["storage_state_mtime"] = datetime.fromtimestamp(
                config.STORAGE_STATE_PATH.stat().st_mtime
            ).isoformat(timespec="seconds")
        if self._context is None:
            return info
        try:
            cookies = await self._context.cookies()
        except Exception:  # noqa: BLE001 - context may be closing
            return info
        expiries = [
            c["expires"] for c in cookies
            if c.get("expires", -1) and c["expires"] > 0
            and "schoology" in (c.get("domain") or "")
        ]
        if expiries:
            soonest = min(expiries)
            info["cookie_expiry_epoch"] = soonest
            info["seconds_to_expiry"] = int(soonest - time.time())
        return info

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

    async def get_text(
        self,
        url: str,
        expect: str | None = None,
        timeout_ms: int = 20_000,
    ) -> tuple[int, str, str]:
        """GET a non-HTML resource with the session's cookies. No page render.

        Returns `(status, content_type, body)`. Used for the iCal feed, which is
        a single fast request compared with a ~3s headless page load.

        `_is_logged_in()` is HTML-specific and cannot judge this response, so
        liveness is checked with `expect` -- a substring the body must contain
        (e.g. "BEGIN:VCALENDAR"). If it is missing we re-login once and retry,
        mirroring `fetch()`.
        """
        async with self._lock:
            await self._ensure_browser()
            for attempt in (1, 2):
                response = await self._context.request.get(url, timeout=timeout_ms)
                body = await response.text()
                content_type = response.headers.get("content-type", "")
                healthy = response.ok and (expect is None or expect in body)
                if healthy:
                    return response.status, content_type, body
                if attempt == 1:
                    log.info(
                        "Feed %s looked wrong (status=%s) -- re-logging in",
                        url, response.status,
                    )
                    await self._login_and_save()
            raise RuntimeError(
                f"Could not fetch {url} after re-login (status={response.status}, "
                f"content-type={content_type!r})."
            )

    async def get_binary(
        self,
        url: str,
        max_bytes: int | None = None,
        timeout_ms: int = 60_000,
    ) -> dict:
        """Download a Schoology-hosted file with the session's cookies."""
        async with self._lock:
            await self._ensure_browser()
            return await self._get_binary(url, max_bytes, timeout_ms)

    async def get_binaries(
        self,
        urls: list[str],
        max_bytes: int | None = None,
        timeout_ms: int = 60_000,
        concurrency: int = 4,
    ) -> list[dict]:
        """Download several files concurrently, taking the lock only once.

        These are plain authenticated HTTP GETs -- no page, no navigation, and
        no re-login retry -- so unlike `fetch()` they cannot race on the login
        flow or on the storage_state write, which is what the lock actually
        protects. Playwright's APIRequestContext handles concurrent requests
        fine.

        Doing this one-at-a-time cost ~1.9s per file; a feed read pulling six
        ~2MB images spent ~11s almost entirely waiting on round trips. Holding
        the lock once for the batch also shortens total lock time, so other
        tools and the keep-alive wait less.
        """
        if not urls:
            return []
        async with self._lock:
            await self._ensure_browser()
            limit = asyncio.Semaphore(max(1, concurrency))

            async def one(url: str) -> dict:
                async with limit:
                    try:
                        return await self._get_binary(url, max_bytes, timeout_ms)
                    except Exception as exc:  # noqa: BLE001 - per-item failure
                        return {"error": str(exc)}

            return await asyncio.gather(*(one(u) for u in urls))

    async def _get_binary(
        self, url: str, max_bytes: int | None, timeout_ms: int
    ) -> dict:
        """Single authenticated GET. Caller must already hold the lock.

        NOTE: `max_bytes` is a memory guard, not a bandwidth one. Playwright has
        no streaming response, so the driver has already buffered the whole body
        by the time Content-Length is visible here -- an oversized file is
        declined *after* being transferred, not before. Only the Drive path
        (rclone --dry-run probe) can refuse ahead of the transfer.
        """
        response = await self._context.request.get(url, timeout=timeout_ms)
        headers = response.headers
        declared = headers.get("content-length")
        size = int(declared) if declared and declared.isdigit() else None

        if max_bytes is not None and size is not None and size > max_bytes:
            return {
                "status": response.status,
                "content_type": headers.get("content-type"),
                "size_bytes": size,
                "too_large": True,
            }
        if not response.ok:
            return {"status": response.status, "error": f"HTTP {response.status}"}

        body = await response.body()
        if max_bytes is not None and len(body) > max_bytes:
            # Reached when no Content-Length was sent; keeps the oversized body
            # out of the cache even though it is already in memory.
            return {
                "status": response.status,
                "content_type": headers.get("content-type"),
                "size_bytes": len(body),
                "too_large": True,
            }
        return {
            "status": response.status,
            "content_type": headers.get("content-type"),
            "size_bytes": len(body),
            "body": body,
            "filename": _filename_from_headers(headers),
        }

    async def campus_json(self, path: str) -> object:
        """GET a JSON resource from Infinite Campus, logging in if needed.

        Infinite Campus rides the same ClassLink SSO as Schoology, so its
        cookies live in this same context -- but they expire independently.
        A response that is not JSON means the portal bounced us to a login
        page, which is the signal to run the tile flow and retry once.
        """
        if not config.CAMPUS_ENABLED:
            raise RuntimeError(
                "Infinite Campus is disabled. Set CAMPUS_ENABLED=true in .env "
                "to turn it on (see .env.example)."
            )

        url = f"{config.CAMPUS_BASE_URL}{path}"
        async with self._lock:
            await self._ensure_browser()
            for attempt in (1, 2):
                response = await self._context.request.get(url, timeout=30_000)
                content_type = response.headers.get("content-type", "")
                if response.ok and "json" in content_type:
                    if attempt == 2:
                        await self._save_state()
                    return await response.json()
                if attempt == 1:
                    log.info("Infinite Campus session missing -- signing in")
                    await auth.login_app(
                        self._context,
                        config.CAMPUS_APP_NAME,
                        r"infinitecampus\.org",
                    )
            raise RuntimeError(
                f"Infinite Campus returned {response.status} ({content_type!r}) "
                f"for {path} even after signing in. If the district moved hosts, "
                "set CAMPUS_BASE_URL; if the portal tile is named differently, "
                "set CAMPUS_APP_NAME."
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


def _filename_from_headers(headers: dict) -> str | None:
    """Pull a filename out of Content-Disposition, if the server sent one."""
    disposition = headers.get("content-disposition") or ""
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition, re.I)
    if not match:
        return None
    from urllib.parse import unquote
    return unquote(match.group(1)).strip() or None


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
