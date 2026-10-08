"""Schoology /v1 REST API over the browser session's cookie.

Schoology documents /v1 as OAuth-1.0a-only, but its own front end calls it with
the plain session cookie -- and so do we. The cookie already lives in
`storage_state.json`, so a warm session reaches the API with **no browser at
all**: this module imports neither Playwright nor `browser.py`, and the
re-login callable is injected precisely so it can stay that way.

Why this exists at all: the HTML path renders a page per tool call, which cost
3.4-4.8s each and returned ~1.5MB to extract a few dozen fields from. The same
data over /v1 is 0.12-0.4s and arrives already structured, so there are no
selectors to rot.

Failure shapes, all verified live 2026-09-01 against the real account:

* **A redirect does not mean the session died.** `/v1/users/me` answers 303 to
  the canonical `/v1/users/<uid>` -- that is ordinary alias resolution, and it
  is why redirects are followed rather than treated as a signal. An *expired*
  session redirects to `/login/saml?destination=...`, which then follows all
  the way out to `login.classlink.com` and returns **HTML with status 200**.
  So liveness is judged by the FINAL response: JSON still under `/v1/` means
  authenticated; anything else means bounced to login. (Same reasoning as
  `browser.campus_json`, and the same reason `_is_logged_in` is content-based.)
* **rate limit -> HTTP 429** with an HTML body. Successful responses carry no
  `X-Rate-Limit-*` header, so the budget had to be measured: from a rested
  bucket the **8th request within 2.4s** was refused, while one request every
  0.5s ran 20 deep clean. A concurrency cap does not bound a *rate* -- twelve
  calls at concurrency 4 sailed through and the next four were refused -- so
  requests are paced through a token bucket as well. The advertised
  `Retry-After: 10` overstates recovery by about 8x (measured 1.2s), and
  honouring it literally turned a 15-call sweep into an 11-second one.
* **`/api/v1/...` is an HTML page.** The API lives at `/v1/...`. Both return
  200, so a wrong prefix fails as a confusing parse error rather than a 404;
  `get()` checks the content type to say so plainly.
* **`/v1/users/me/notifications` does not 404 or 400** -- it silently returns
  the `/v1/users/me` payload (the user profile). Don't trust a 200 alone.
"""

import asyncio
import json
import logging
import re
from urllib.parse import unquote

import httpx

from . import config

log = logging.getLogger("schoology_mcp.api")

# Eleven unbounded parallel calls returned three 429s. Measured clean runs:
# conc=2 1.00s, conc=3 0.71s, conc=4 0.65s, conc=6 0.42s -- all 200.
#
# Re-measured 2026-09-02: the ceiling is a *burst quota*, not a concurrency cap.
# 12 requests fired at once all returned 200 in 0.51s; 16, 24 and 40 at once
# each returned exactly 15 x 200 and refused the rest. 60 requests at
# concurrency 8 also stopped at 15, which is how we know 15 is a token count
# and not a limit on requests in flight. Eight keeps every tool's fan-out in
# one round trip while staying well under the quota.
MAX_CONCURRENCY = 8

# Proactive pacing, because a 429 is far more expensive than waiting.
#
# Re-measured 2026-09-02 against the live account; the earlier reading of "the
# 8th request in 2.4s is refused" did not reproduce and the numbers below
# replace it. The server's own bucket holds **15 tokens**: bursts of 16/24/40
# were each served exactly 15 times, and a refusal cleared after 0.5s. A serial
# run of 30 requests at 3.0 req/s was never refused at all.
#
# The old capacity of 6 was the whole latency problem. Every tool here fans out
# past it in a single call -- check_updates issues 8, get_grades(detailed) 10,
# get_upcoming_assignments(include_info) 11 -- so from the 7th request onward
# each one sat waiting 0.5s for a token that the server would have granted
# immediately. Measured share of wall clock spent waiting on our own pacer,
# not the network: get_grades(detailed) 4.94s -> 0.96s with the pacer off,
# check_updates 3.99s -> 0.79s. A capacity of 12 covers every tool's fan-out in
# one burst and still leaves 3 tokens of headroom under the measured 15.
#
# The *refill* stays at the old 0.5s. Capacity is what a single tool call
# spends and refill is what back-to-back calls share, and only the first was
# mispriced: raising refill to 0.35s (2.9/s) drew real 429s in a no-think-time
# benchmark, which puts the server's own refill below that. The observations
# only bracket it -- 30 serial requests over 10.07s from a full bucket were
# never refused, so it is at least 1.5/s -- and 2/s has run clean throughout.
# Binary downloads ask for no compression. Schoology answers a gzip-encoded
# request with `Transfer-Encoding: chunked` and NO Content-Length, which defeats
# the whole point of streaming: the size cannot be checked before the transfer,
# and an aborted download can only report how much arrived, not how big the file
# is. With `identity` the same PDF declares `Content-Length: 480793` up front.
# Nothing is lost: measured on two real PDFs, identity was the same speed or
# slightly faster (0.20s vs 0.25s), because these files are already compressed.
_BINARY_HEADERS = {"Accept-Encoding": "identity"}

_BUCKET_CAPACITY = 12
_BUCKET_REFILL_SECONDS = 0.5

# `Retry-After: 10` is what Schoology sends, but the limit measurably lifts far
# sooner: 1.2s when first measured, and 0.5s on the 2026-09-02 re-measurement
# (one probe at +0.5s after a refusal already answered 200). Honouring the
# header literally turned a 15-call sweep into an 11-second one, so it is
# capped and retried rather than believed outright.
#
# The cap is the thing that actually shows up in a slow call. A refusal is rare
# and recovers in half a second, but the old 3.0s cap turned each one into a
# three-second stall -- that alone was the difference between a 0.6s and a 5.4s
# get_grades(detailed=True). At 1.0s there is still 2x headroom over the
# measured recovery, and three tries bound the worst case at ~3s.
_RETRY_AFTER_CAP = 1.0
_MAX_RATE_RETRIES = 3


class _TokenBucket:
    """Paces requests so the server never has to refuse one.

    Deliberately not a semaphore: concurrency is already capped, and the limit
    being defended here is a *rate*, which a concurrency cap does not bound --
    twelve calls at concurrency 4 sail through, and the next four immediately
    run into a 429 because they land inside the same window.
    """

    def __init__(self, capacity: int, refill_seconds: float) -> None:
        self._capacity = capacity
        self._refill = refill_seconds
        self._tokens = float(capacity)
        self._updated = None
        self._lock = asyncio.Lock()

    async def take(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if self._updated is None:
                self._updated = now
            self._tokens = min(
                self._capacity,
                self._tokens + (now - self._updated) / self._refill,
            )
            self._updated = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) * self._refill
                await asyncio.sleep(wait)
                self._updated = loop.time()
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


def _is_api_json(response) -> bool:
    """True when a response is a live /v1 JSON answer rather than a login page.

    Both conditions matter. Content-type alone would accept a JSON error body
    served from somewhere else, and path alone would accept the HTML login page
    if Schoology ever bounced within /v1.
    """
    if "json" not in response.headers.get("content-type", ""):
        return False
    return response.url.path.startswith("/v1/")


class SessionExpired(RuntimeError):
    """A /v1 call was redirected to the login page."""


class SchoologyAPI:
    """Cookie-authenticated /v1 client. Owns no browser."""

    def __init__(self, relogin=None) -> None:
        # `relogin` is an async callable that refreshes storage_state.json.
        # Injected rather than imported so this module never pulls in
        # Playwright -- that is the whole point of it existing.
        self._relogin = relogin
        self._client: httpx.AsyncClient | None = None
        self._sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self._bucket = _TokenBucket(_BUCKET_CAPACITY, _BUCKET_REFILL_SECONDS)
        self._uid: str | None = None
        # Serializes re-login so a burst of 302s triggers one browser launch,
        # not one per in-flight request.
        self._relogin_lock = asyncio.Lock()
        self._relogin_count = 0

    # -- cookies -----------------------------------------------------------

    @staticmethod
    def cookies_from_storage_state() -> dict[str, str]:
        """Read the Schoology cookies straight out of the Playwright state file.

        Reading the file rather than asking a live browser for its cookies is
        what lets a warm session skip Chromium entirely.
        """
        path = config.STORAGE_STATE_PATH
        if not path.exists():
            return {}
        try:
            state = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            # Mirrors browser.py: a half-written state file costs one login,
            # it must never make the server unusable.
            log.warning("Unreadable %s (%s); starting cookieless", path, exc)
            return {}
        return {
            c["name"]: c["value"]
            for c in state.get("cookies", [])
            if "schoology" in (c.get("domain") or "")
        }

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=config.BASE_URL,
                cookies=self.cookies_from_storage_state(),
                timeout=30.0,
                # Followed on purpose: /v1/users/me answers 303 to the
                # canonical /v1/users/<uid>. Expiry is detected from the final
                # response instead (see `_is_api_json`).
                follow_redirects=True,
            )
        return self._client

    async def _reload_cookies(self) -> None:
        client = await self._ensure_client()
        client.cookies.clear()
        for name, value in self.cookies_from_storage_state().items():
            client.cookies.set(name, value)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- requests ----------------------------------------------------------

    async def _refresh_session(self, generation: int) -> None:
        """Re-login once, even if several requests raced into a 302.

        `generation` is the `_relogin_count` the caller observed before it hit
        the redirect. If it has already moved, someone else refreshed while we
        waited for the lock and there is nothing left to do -- otherwise ten
        parallel calls on a dead session would launch ten browsers.
        """
        async with self._relogin_lock:
            if self._relogin_count != generation:
                return
            await self._relogin()
            await self._reload_cookies()
            self._relogin_count += 1

    async def get(self, path: str, _retried: bool = False, _rate_tries: int = 0) -> object:
        """GET a /v1 resource, returning parsed JSON.

        Paced by the token bucket, re-logs in once on an expired session, and
        backs off on a 429 if one slips through anyway.
        """
        client = await self._ensure_client()
        generation = self._relogin_count
        async with self._sem:
            await self._bucket.take()
            response = await client.get(path)

        if response.status_code == 429 and _rate_tries < _MAX_RATE_RETRIES:
            # The bucket should make this rare. When it does happen, the
            # advertised Retry-After (10s) is roughly 8x the delay actually
            # needed -- the limit lifted after 1.2s in measurement -- so wait a
            # capped amount and try again rather than stalling the whole batch.
            delay = _retry_after_seconds(response.headers.get("retry-after"))
            log.info(
                "Rate limited on %s; sleeping %.1fs (attempt %d/%d)",
                path, delay, _rate_tries + 1, _MAX_RATE_RETRIES,
            )
            await asyncio.sleep(delay)
            return await self.get(path, _retried=_retried, _rate_tries=_rate_tries + 1)

        # A dead session shows up in two different shapes, neither of them an
        # exception by default:
        #   * no cookie at all  -> 302 to /login/saml, followed out to
        #     login.classlink.com, arriving as HTML with status 200
        #   * a stale or invalid cookie -> 401 on the /v1 URL itself
        # 403 is deliberately NOT in that set: it is a real permission denial
        # (/v1/sections/<id>/grades answers 403 for a student account), and
        # re-logging in would just loop.
        bounced = response.is_success and not _is_api_json(response)
        if bounced or response.status_code == 401:
            if _retried or self._relogin is None:
                raise SessionExpired(
                    f"{path} is not authenticated "
                    f"({'bounced to ' + str(response.url) if bounced else 'HTTP 401'})"
                    + (" even after re-login" if _retried else "")
                    + ". Run scripts/login_check.py --show-browser to inspect."
                )
            log.info("Session expired on %s -- re-logging in", path)
            await self._refresh_session(generation)
            return await self.get(path, _retried=True)

        response.raise_for_status()
        if not _is_api_json(response):
            raise RuntimeError(
                f"{path} returned {response.headers.get('content-type')!r} from "
                f"{response.url}, not /v1 JSON. The API lives at /v1/... -- note "
                "that /api/v1/... serves an HTML page with a 200 status."
            )
        return response.json()

    async def get_many(self, paths: list[str]) -> list:
        """Fetch several resources concurrently.

        Per-path failures come back as exception objects instead of raising:
        one dead course must never abort a materials sweep, and one failed
        source must never abort `check_updates`.
        """
        if not paths:
            return []
        return await asyncio.gather(
            *(self.get(p) for p in paths), return_exceptions=True
        )

    # -- binaries ----------------------------------------------------------

    async def get_binary(
        self,
        url: str,
        max_bytes: int | None = None,
        timeout_ms: int = 60_000,
        _retried: bool = False,
    ) -> dict:
        """Download a file behind the school login.

        Deliberately the same contract as `browser.SchoologyClient.get_binary`
        -- `{status, content_type, size_bytes, body, filename}`, or `too_large`,
        or `error` -- so `downloads.py` takes either object without knowing
        which. It is the client that changed, not the cache.

        **Here `max_bytes` really is a bandwidth guard.** Playwright has no
        streaming response, so on that path the driver has already buffered the
        whole body before Content-Length is visible and an oversized file is
        declined only *after* being transferred. httpx streams, so an oversized
        file is refused from its headers, before any of it crosses the wire, and
        a body that lies about its length is abandoned partway through.

        Attachments live on `api.<host>` rather than the main host, which the
        session cookie covers, and they are plain authenticated GETs with no
        page and no navigation -- so, as on the browser path, there is no
        re-login retry here. What there *is* is a check that we were not handed
        a login page instead of a file: caching an HTML redirect under the name
        `Course Guide.pdf` would be worse than failing.
        """
        client = await self._ensure_client()
        generation = self._relogin_count
        chunks: list[bytes] = []
        # No token bucket here, unlike `get()`. The measured rate limit applies
        # to the /v1 API; these are static files, usually from the asset CDN on
        # a different host entirely. Paced through the same bucket, ten course
        # logos took 4.72s against 0.14s unpaced -- a limiter defending a budget
        # these requests do not spend. The concurrency cap still applies.
        async with self._sem:
            timeout = httpx.Timeout(timeout_ms / 1000)
            try:
                async with client.stream(
                    "GET", url, timeout=timeout, headers=_BINARY_HEADERS
                ) as response:
                    headers = response.headers
                    declared = headers.get("content-length")
                    size = int(declared) if declared and declared.isdigit() else None

                    if max_bytes is not None and size is not None and size > max_bytes:
                        # Refused from the headers alone -- nothing downloaded.
                        await response.aclose()
                        return {
                            "status": response.status_code,
                            "content_type": headers.get("content-type"),
                            "size_bytes": size,
                            "too_large": True,
                        }
                    if not response.is_success:
                        return {
                            "status": response.status_code,
                            "error": f"HTTP {response.status_code}",
                        }
                    if _looks_like_login(response):
                        await response.aclose()
                        expired = True
                    else:
                        expired = False

                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if max_bytes is not None and total > max_bytes:
                            # Reached only when Content-Length was absent or
                            # wrong. Abandoning mid-stream still saves whatever
                            # has not been sent yet.
                            # Backstop for a response that declared no length.
                            # `total` is only what arrived before giving up, so
                            # it is reported as a floor -- quoting it as "the"
                            # size would understate the file and mislead
                            # whoever decides whether to raise the limit.
                            await response.aclose()
                            return {
                                "status": response.status_code,
                                "content_type": headers.get("content-type"),
                                "size_bytes": total,
                                "size_is_lower_bound": True,
                                "too_large": True,
                            }
                        chunks.append(chunk)
            except httpx.HTTPError as exc:
                return {"error": f"{type(exc).__name__}: {exc}"}

        if expired:
            # The browser client does not retry here, but it costs nothing to:
            # caching an HTML redirect under the name "Course Guide.pdf" is a
            # failure that only shows up much later, when someone opens it.
            if _retried or self._relogin is None:
                raise SessionExpired(
                    f"{url} served a login page instead of a file"
                    + (" even after re-login" if _retried else "")
                )
            log.info("Session expired downloading %s -- re-logging in", url)
            await self._refresh_session(generation)
            return await self.get_binary(url, max_bytes, timeout_ms, _retried=True)

        body = b"".join(chunks)
        return {
            "status": response.status_code,
            "content_type": headers.get("content-type"),
            "size_bytes": len(body),
            "body": body,
            "filename": _filename_from_headers(headers),
        }

    async def get_binaries(
        self,
        urls: list[str],
        max_bytes: int | None = None,
        timeout_ms: int = 60_000,
        concurrency: int = 4,
    ) -> list[dict]:
        """Download several files concurrently, preserving order.

        `concurrency` is accepted to match the browser client's signature but is
        not used: `_sem` and the token bucket already bound how many of these
        are in flight, and adding a second limiter on top would only make the
        effective one harder to reason about.
        """
        if not urls:
            return []

        async def one(url: str) -> dict:
            try:
                return await self.get_binary(url, max_bytes, timeout_ms)
            except Exception as exc:  # noqa: BLE001 - per-item failure
                return {"error": str(exc)}

        return await asyncio.gather(*(one(u) for u in urls))

    async def uid(self) -> str:
        """The signed-in student's numeric id, cached for the process lifetime."""
        if self._uid is None:
            me = await self.get("/v1/users/me")
            if not isinstance(me, dict) or "id" not in me:
                raise RuntimeError(f"/v1/users/me returned no id: {me!r:.200}")
            self._uid = str(me["id"])
        return self._uid


def _retry_after_seconds(raw, default: float = 1.5) -> float:
    """Parse Retry-After, capped well below what Schoology asks for.

    It advertises 10s; the limit measurably lifted after 1.2s. Waiting the full
    header turned a 15-call sweep into an 11-second one, so the value is capped
    and the caller retries a few times instead of trusting it.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return min(max(value, 0.5), _RETRY_AFTER_CAP)


def _looks_like_login(response) -> bool:
    """True when a file request came back as the SSO login page.

    Checked on the host rather than the content type: an attachment could
    legitimately be HTML, but it will never be served from ClassLink.
    """
    host = response.url.host or ""
    return "classlink.com" in host or "/login" in (response.url.path or "")


def _filename_from_headers(headers) -> str | None:
    """Pull a filename out of Content-Disposition, if the server sent one.

    A near-copy of the browser client's helper, deliberately: this module does
    not import that one, which is what keeps the warm path free of Playwright.
    """
    disposition = headers.get("content-disposition") or ""
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition, re.I)
    if not match:
        return None
    return unquote(match.group(1)).strip() or None
