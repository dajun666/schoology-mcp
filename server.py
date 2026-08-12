"""Schoology MCP server.

Exposes tools that drive a headless browser to log into PAUSD Schoology (via
the ClassLink portal) and return grades, upcoming assignments and recent posts.

Run directly:  python server.py   (communicates over stdio)
"""

import logging
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime

from typing import Any

from mcp.server.fastmcp import FastMCP

from schoology_mcp import config, downloads, gdocs, health, ical, parsers
from schoology_mcp.browser import SchoologyClient

_ASSIGNMENT_ID_RE = re.compile(r"/assignment/(\d+)")
_COURSE_ID_RE = re.compile(r"/course/(\d+)")
_PAGE_ID_RE = re.compile(r"/page/(\d+)")


def _path_from(url_or_id: str, pattern: re.Pattern, template: str, label: str) -> str:
    """Normalize a full URL, a relative path, or a bare numeric id to a path.

    One implementation so every tool accepts exactly the same input shapes --
    three near-copies meant a fix for, say, a trailing `?query` landed in one
    and left the others rejecting URLs the neighbouring tool accepted.
    """
    s = (url_or_id or "").strip()
    if s.isdigit():
        return template.format(s)
    m = pattern.search(s)
    if m:
        return template.format(m.group(1))
    raise ValueError(f"Not a recognizable Schoology {label} URL/ID: {url_or_id!r}")


def _course_materials_path(url_or_id: str) -> str:
    return _path_from(url_or_id, _COURSE_ID_RE, "/course/{}/materials", "course")


def _assignment_path(url_or_id: str) -> str:
    return _path_from(url_or_id, _ASSIGNMENT_ID_RE, "/assignment/{}", "assignment")

# MCP uses stdout for the protocol -- all logging MUST go to stderr.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

client = SchoologyClient()


@asynccontextmanager
async def lifespan(_server):
    try:
        yield
    finally:
        await client.close()


mcp = FastMCP("schoology", lifespan=lifespan)


@mcp.tool()
async def get_grades(detailed: bool = False) -> dict[str, Any]:
    """Get the student's current grades.

    Default: one entry per course with its overall grade -- small and quick.
    Pass `detailed=True` to also include every grading period, category and
    individual assignment row (the full payload, often hundreds of rows).
    """
    html = await client.fetch("/grades/grades", wait_selector="li.s-grades-course-item")
    courses = parsers.parse_grades(html, config.BASE_URL)
    if not detailed:
        courses = [
            {"title": c["title"], "course_grade": c["course_grade"]}
            for c in courses
        ]
    return {
        "base_url": config.BASE_URL,
        "courses": courses,
    }


@mcp.tool()
async def get_courses() -> dict[str, Any]:
    """Get the list of courses the student is enrolled in."""
    html = await client.fetch("/courses", wait_selector="li.course-item, div.course-card")
    return {
        "base_url": config.BASE_URL,
        "courses": parsers.parse_courses(html, config.BASE_URL),
    }


@mcp.tool()
async def get_upcoming_assignments(days: int = 14, include_info: bool = False) -> dict[str, Any]:
    """Get upcoming / due-soon assignments and events.

    `days` is an advisory window; the home widget already shows near-term
    items, so all of them are returned for the model to filter.

    `include_info=True` additionally fetches each assignment's description /
    attachments (one extra page load per item -- noticeably slower). Off by
    default so the common case stays cheap; turn it on when the model needs
    to read what the assignment is actually asking for.
    """
    html = await client.fetch("/home", extra_wait_ms=3_000)
    assignments = parsers.parse_upcoming_assignments(html, config.BASE_URL)
    if include_info:
        for a in assignments:
            url = a.get("url")
            if not url or "/assignment/" not in url:
                # /event/... items have different markup -- skip.
                continue
            try:
                detail = await get_assignment_info(url)
                a["description"] = detail.get("description")
                a["description_html"] = detail.get("description_html")
                a["attachments"] = detail.get("attachments")
            except Exception as exc:  # noqa: BLE001 - one bad page must not abort the batch
                a["info_error"] = str(exc)
    return {
        "base_url": config.BASE_URL,
        "window_days": days,
        "assignments": assignments,
    }


@mcp.tool()
async def get_assignment_info(url_or_id: str) -> dict[str, Any]:
    """Fetch a single Schoology assignment's full details.

    Accepts a full URL, a relative path like `/assignment/123`, or the bare
    numeric assignment id. Returns title, course, due date, description body
    (text + HTML) and any attached files.
    """
    path = _assignment_path(url_or_id)
    html = await client.fetch(
        path, wait_selector=".info-body, #main h1, .page-title"
    )
    info = parsers.parse_assignment_info(html, config.BASE_URL)
    info["url"] = f"{config.BASE_URL}{path}"
    return info


@mcp.tool()
async def get_course_materials(
    course_id_or_url: str,
    include_folder_contents: bool = True,
) -> dict[str, Any]:
    """Get the materials list for a Schoology course.

    Accepts a full course URL, a relative path like `/course/123`, or the bare
    numeric course id. Returns all top-level materials (folders, assignments,
    documents, pages, links). With `include_folder_contents=True` (default)
    each folder's contents are fetched too and returned with a `folder` field
    indicating which folder they came from.
    """
    root_path = _course_materials_path(course_id_or_url)
    html = await client.fetch(
        root_path, wait_selector="#folder-contents-table, .s-js-materials-body"
    )
    top_level = parsers.parse_course_materials(html, config.BASE_URL)

    materials = []
    for item in top_level:
        item["folder"] = None
        if item["type"] == "folder" and include_folder_contents and item.get("url"):
            folder_title = item["title"]
            # Derive the ?f=NNN path from the full URL for client.fetch()
            folder_path = item["url"].replace(config.BASE_URL, "")
            try:
                folder_html = await client.fetch(
                    folder_path,
                    wait_selector="#folder-contents-table, .s-js-materials-body",
                )
                for child in parsers.parse_course_materials(folder_html, config.BASE_URL):
                    child["folder"] = folder_title
                    materials.append(child)
            except Exception as exc:  # noqa: BLE001
                item["fetch_error"] = str(exc)
                materials.append(item)
        else:
            materials.append(item)

    return {
        "base_url": config.BASE_URL,
        "course_url": f"{config.BASE_URL}{root_path}",
        "materials": materials,
    }


@mcp.tool()
async def get_material(
    url: str,
    export_google_docs: bool = True,
    allow_binary: bool = False,
) -> dict[str, Any]:
    """Open and read a single Schoology material by URL.

    Accepts any material URL returned by `get_course_materials`. Dispatches
    automatically by URL type:
      - /assignment/NNN        → full assignment details (description, due, attachments)
      - /page/NNN              → page title and body text
      - /materials/gp/NNN      → file/document with download and viewer URLs
      - /materials/link/view/NNN → external link title and resolved URL

    When a link points at Google Docs/Drive, the document is exported to
    Markdown and returned under `google_doc` (with any embedded images written
    out as files you can open). Set `export_google_docs=False` to skip that.

    Uploaded files (PDFs, images, video) are reported but NOT downloaded --
    their size is unknown in advance and one real assignment linked a 110 MB
    video. Pass `allow_binary=True` to fetch them anyway.

    Export failures are reported in `google_doc.export_error` rather than
    failing the call.

    Pass the full URL (https://...) or a relative path.
    """
    url = url.strip()
    # Normalize to a relative path for fetching.
    path = url.replace(config.BASE_URL, "")

    if "/assignment/" in path:
        path = _assignment_path(path)
        html = await client.fetch(path, wait_selector=".info-body, #main h1, .page-title")
        info = parsers.parse_assignment_info(html, config.BASE_URL)
        info["url"] = f"{config.BASE_URL}{path}"
        return info

    if "/page/" in path:
        html = await client.fetch(path, wait_selector="h1.page-title, .s-rte")
        return parsers.parse_page_content(html, config.BASE_URL)

    if "/materials/link/view/" in path:
        html = await client.fetch(path, wait_selector="h1.page-title")
        info = parsers.parse_link_info(html, config.BASE_URL)
        # Teachers often attach a Google Doc rather than uploading a file, in
        # which case the "material" is just an external link and the reader
        # gets nothing. Export it so the content is actually readable.
        if export_google_docs and gdocs.is_drive_url(info.get("url")):
            info["google_doc"] = await gdocs.fetch_document(
                info["url"], allow_binary=allow_binary
            )
        return info

    if "/materials/gp/" in path or "/materials/" in path:
        html = await client.fetch(path, wait_selector="h1.page-title, .attachments")
        return parsers.parse_document_info(html, config.BASE_URL)

    raise ValueError(f"Unrecognized Schoology material URL: {url!r}")


async def _fetch_post_images(posts: list[dict], max_bytes: int) -> int:
    """Download each post's embedded images into the cache, in place.

    School notices are frequently posted as a picture with no words, so the
    image often IS the post. They need the session cookie, and one unreachable
    image must not spoil the batch -- failures attach `error` to that image and
    the rest carry on, matching the info_error / fetch_error convention.
    """
    images = [img for post in posts for img in (post.get("images") or [])]
    if not images:
        return 0

    results = await downloads.fetch_many(client, [i["url"] for i in images], max_bytes)
    saved = 0
    for image, result in zip(images, results):
        image.update(result)
        if "path" in result and not result.get("cached"):
            saved += 1
    return saved


@mcp.tool()
async def get_recent_posts(
    limit: int = 20,
    download_images: bool = True,
    max_image_mb: float = 10.0,
) -> dict[str, Any]:
    """Get the latest posts from the Schoology activity feed, images included.

    Posts routinely consist of nothing but an image — a flyer, a screenshot of
    a notice — so the pictures are downloaded by default and each one comes back
    with a `path` you can open. Emoji are folded back into the text as
    characters, and profile avatars are ignored.

    Set `download_images=False` to get the image URLs without fetching them.
    """
    html = await client.fetch("/home", extra_wait_ms=3_000)
    posts = parsers.parse_recent_posts(html, config.BASE_URL, limit=limit)

    downloaded = 0
    if download_images:
        downloaded = await _fetch_post_images(posts, int(max_image_mb * 1024 * 1024))

    return {
        "base_url": config.BASE_URL,
        "posts": posts,
        "image_count": sum(len(p.get("images") or []) for p in posts),
        "images_downloaded": downloaded,
    }


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------

def _message_path(url_or_id: str) -> str:
    return _path_from(
        url_or_id, parsers.MESSAGE_ID_RE, "/messages/view/{}", "message"
    )


@mcp.tool()
async def get_messages(
    folder: str = "inbox",
    limit: int = 25,
    unread_only: bool = False,
) -> dict[str, Any]:
    """List private messages (teacher/staff mail) without opening any of them.

    Teachers send deadline reminders, forms and course logistics here, and none
    of it shows up in the activity feed. Returns subject, sender, date and a
    body preview per thread -- enough to decide what matters. Listing does not
    mark anything read.

    `folder` is "inbox" or "sent". `unread_count` comes from the page header
    and is the reliable unread signal; the per-message `unread` flag is best
    effort and may be None (see `parsers.parse_messages`).
    """
    if folder not in ("inbox", "sent"):
        raise ValueError(f"folder must be 'inbox' or 'sent', got {folder!r}")
    path = "/messages" if folder == "inbox" else "/messages/sent"

    html = await client.fetch(path, wait_selector="table.privatemsg-list")
    messages = parsers.parse_messages(html, config.BASE_URL)
    counters = parsers.parse_header_counters(html)

    # With zero unread overall, every listed row is definitively read -- more
    # trustworthy than the per-row guess.
    if counters.get("unread_messages") == 0:
        for m in messages:
            m["unread"] = False

    status = health.evaluate(
        html,
        anchors=parsers.MESSAGES_ANCHORS,
        empty_markers=parsers.MESSAGES_EMPTY_MARKERS,
        item_count=len(messages),
    )

    if unread_only:
        messages = [m for m in messages if m.get("unread")]

    return {
        "base_url": config.BASE_URL,
        "folder": folder,
        "unread_count": counters.get("unread_messages"),
        "health": status,
        "count": len(messages),
        "messages": messages[:limit],
    }


@mcp.tool()
async def get_message_thread(url_or_id: str) -> dict[str, Any]:
    """Read one message thread in full. WARNING: THIS MARKS IT READ.

    Opening a thread clears its unread badge on Schoology's side, exactly as if
    the student had clicked it. That is the only server-visible side effect in
    this whole MCP -- everything else is a pure read. Use it when you actually
    need a message's full text; use `get_messages` to browse.

    Never call this from a scheduled/automated check.

    Accepts a full URL, `/messages/view/123`, or the bare numeric thread id.
    """
    path = _message_path(url_or_id)
    html = await client.fetch(path, wait_selector=".message-body", extra_wait_ms=1_500)
    thread = parsers.parse_message_thread(html, config.BASE_URL)
    thread["url"] = f"{config.BASE_URL}{path}"
    thread["marked_read"] = True
    return thread


# --------------------------------------------------------------------------
# Calendar (iCal feed)
# --------------------------------------------------------------------------

# Discovered feed URL, cached for the process lifetime only. This is not
# change-detection state -- it does not affect any output and is re-derived on
# restart. Nothing is written to disk. Tools also return `ical_url` and accept
# it back, so a caller can skip discovery entirely on later runs.
_ICAL_URL: str | None = None

# The feed carries every event Schoology has ever published to this student
# (709 at PAUSD, back to 2013). Never hand a model the whole thing.
_MAX_EVENTS = 200


async def _resolve_ical_url(override: str | None = None, page_html: str | None = None) -> str:
    """Find the .ics feed URL: any page -> export dialog -> webcal -> https.

    The numeric user id is embedded in every authenticated page, so when the
    caller already holds one (check_updates fetches /home) that page is used and
    a whole extra load is avoided.
    """
    global _ICAL_URL
    if override:
        return override
    if _ICAL_URL:
        return _ICAL_URL

    html = page_html or await client.fetch("/calendar", extra_wait_ms=1_500)
    user_id = ical.user_id_from_html(html)
    if not user_id and page_html:
        html = await client.fetch("/calendar", extra_wait_ms=1_500)
        user_id = ical.user_id_from_html(html)
    if not user_id:
        raise RuntimeError(
            "Could not find the Schoology user id on /calendar, so the iCal "
            "feed URL cannot be derived."
        )
    export_html = await client.fetch(ical.export_path(user_id), extra_wait_ms=1_500)
    url = ical.feed_url_from_export_html(export_html, config.BASE_URL)
    if not url:
        raise RuntimeError(
            "The calendar export dialog did not contain an .ics URL. Schoology "
            "may have changed the export flow."
        )
    _ICAL_URL = url
    return url


async def _load_calendar(
    ical_url: str | None = None, page_html: str | None = None
) -> tuple[str, list[dict], dict]:
    """Fetch and parse the whole feed. Returns (url, events, fetch_health)."""
    url = await _resolve_ical_url(ical_url, page_html)
    try:
        status, content_type, body = await client.get_text(
            url, expect="BEGIN:VCALENDAR"
        )
    except Exception as exc:  # noqa: BLE001 - a stale cached URL must self-heal
        global _ICAL_URL
        if ical_url is None and _ICAL_URL:
            _ICAL_URL = None  # force rediscovery on the next call
        raise RuntimeError(f"iCal feed fetch failed: {exc}") from exc

    events = ical.parse_ics(body)
    return url, events, {
        "http_status": status,
        "content_type": content_type,
        "bytes": len(body),
        "total_events": len(events),
    }


@mcp.tool()
async def get_calendar_events(
    days_ahead: int = 14,
    days_back: int = 0,
    query: str | None = None,
    ical_url: str | None = None,
) -> dict[str, Any]:
    """Get school calendar events (holidays, no-school days, deadlines, rallies).

    Reads Schoology's iCal feed -- one HTTP request, no browser rendering, so
    this is by far the cheapest source in this server.

    `days_back`/`days_ahead` window around today. `query` filters by substring
    over title and description. Pass `ical_url` (returned by every call) to skip
    feed rediscovery.

    NOTE: at PAUSD this feed contains school *events* only -- no assignment due
    dates. Use `get_upcoming_assignments` for those.
    """
    url, events, feed_health = await _load_calendar(ical_url)
    selected = ical.search(ical.window(events, days_back, days_ahead), query)

    truncated = len(selected) > _MAX_EVENTS
    return {
        "base_url": config.BASE_URL,
        "ical_url": url,
        "window": {"days_back": days_back, "days_ahead": days_ahead},
        "query": query,
        "health": {**feed_health, "in_window": len(selected)},
        "truncated": truncated,
        "events": selected[:_MAX_EVENTS],
    }


@mcp.tool()
async def download_file(url: str, max_mb: float = 25.0) -> dict[str, Any]:
    """Download a file linked from Schoology and say where it landed.

    Give it any link you found in a material, assignment description or message:

      - Google Docs/Sheets/Slides → exported (Docs come back as Markdown text in
        `content`, with embedded images written out as openable files)
      - Uploaded Drive files (PDF, image, video) → downloaded as-is, path returned
      - Schoology-hosted attachments → downloaded with the logged-in session

    Anything over `max_mb` is declined with its real size reported — a real
    assignment linked a 110 MB video. For Drive that check happens *before* any
    transfer (a metadata-only probe); for Schoology attachments the response is
    already buffered by the time its size is known, so the limit keeps it out of
    the cache rather than off the wire. Raise `max_mb` if you want the file.

    Files land in a temp cache and are pruned after 24h, so treat `path` as
    something to read now, not somewhere to keep things.

    Only Google Drive and Schoology URLs are accepted; this is not a general
    web downloader.
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("url is required")
    max_bytes = int(max_mb * 1024 * 1024)

    if gdocs.is_drive_url(url):
        # fetch_document owns the whole Drive pre-flight -- kind check, id
        # parsing, size probe -- so this stays a dispatcher rather than a second
        # copy of those rules.
        result = await gdocs.fetch_document(
            url, allow_binary=True, max_bytes=max_bytes
        )
        result["source"] = "drive"
        result.setdefault("url", url)
        # `fetch_document` reports failures as `export_error` (it is one step of
        # reading a material); for a tool whose whole job is the download, the
        # failure belongs under the conventional `error` key.
        if "export_error" in result:
            result["error"] = result.pop("export_error")
            if result.get("declined") == "too_large":
                result["error"] += " Raise max_mb to fetch it."
        return result

    if config.is_schoology_url(url):
        target = parsers.absolute_url(url, config.BASE_URL)
        result = await downloads.fetch_to_cache(client, target, max_bytes)
        return {"source": "schoology", "url": target, **result}

    return {
        "url": url,
        "error": (
            "Only Google Drive and Schoology URLs are supported. This tool "
            "exists to fetch things behind the school login, not to browse the "
            "web."
        ),
    }


# --------------------------------------------------------------------------
# Change detection (stateless: the caller owns the baseline)
# --------------------------------------------------------------------------


async def _grab(path: str, wait_selector: str | None = None, extra_wait_ms: int = 2_000):
    """Fetch a page, returning (html, error). One bad source must not abort the run."""
    try:
        html = await client.fetch(path, wait_selector=wait_selector, extra_wait_ms=extra_wait_ms)
        return html, None
    except Exception as exc:  # noqa: BLE001 - reported as source health, not raised
        return None, str(exc)


@mcp.tool()
async def check_updates(
    baseline: dict | str | None = None,
    sources: list[str] | None = None,
    include_details: bool = True,
    max_detail_items: int = 25,
    calendar_days_ahead: int = 14,
    calendar_days_back: int = 0,
    ical_url: str | None = None,
    detailed_sources: list[str] | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """What changed since last time (grades, assignments, messages, calendar).

    Stateless: `baseline` is YOUR memory of the last run. Pass back the
    `baseline` this returned previously to get a real diff; omit it for a
    snapshot with nothing announced.

    Each run: call with your stored baseline -> notify from `alerts` -> THEN
    store the new baseline. If the reply says `baseline_unchanged`, keep the one
    you have; a new `baseline` key appears only when something actually moved.

    Silent by design on: first run, failed/suspect sources (your baseline for
    them is preserved), and items merely expiring off a list. Never opens a
    message thread, so it never marks mail read.

    Sources: grades, upcoming, messages, calendar, posts. Unrequested ones pass
    through untouched. `verbose=True` adds per-source diagnostics.
    """
    from schoology_mcp import fingerprint, watch

    requested = list(sources) if sources else list(watch.DEFAULT_SOURCES)
    unknown = [s for s in requested if s not in watch.ALL_SOURCES]
    if unknown:
        raise ValueError(
            f"Unknown source(s) {unknown}. Valid: {list(watch.ALL_SOURCES)}"
        )

    prior = fingerprint.coerce_baseline(baseline)
    prior_sources = fingerprint.baseline_sources(prior)
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")

    def prev_count(name: str):
        entry = prior_sources.get(name)
        if isinstance(entry, dict) and isinstance(entry.get("value"), dict):
            return entry["value"].get("count")
        return None

    collected: dict[str, tuple] = {}
    notes: list[str] = []
    resolved_ical = ical_url

    # /home serves upcoming assignments, the activity feed AND the header
    # unread counters -- fetch it once, not three times.
    home_html = home_err = None
    if {"upcoming", "posts"} & set(requested):
        # The fixed 3s settle exists for the JS-rendered activity feed; the
        # upcoming widget is in the server HTML, so a run without `posts`
        # (the scheduled default) should not pay it.
        await_feed = 3_000 if "posts" in requested else 0
        home_html, home_err = await _grab("/home", extra_wait_ms=await_feed)

    if "grades" in requested:
        html, err = await _grab("/grades/grades", wait_selector="li.s-grades-course-item")
        courses = parsers.parse_grades(html, config.BASE_URL) if html else []
        rows = watch.flatten_grades(courses)
        collected["grades"] = (
            rows,
            health.evaluate(
                html, anchors=parsers.GRADES_ANCHORS,
                empty_markers=parsers.GRADES_EMPTY_MARKERS,
                item_count=len(rows), prev_count=prev_count("grades"), error=err,
            ),
            {"id_key": "uid", "meta_key": "fp_meta"},
        )

    if "upcoming" in requested:
        items = parsers.parse_upcoming_assignments(home_html, config.BASE_URL) if home_html else []
        collected["upcoming"] = (
            items,
            health.evaluate(
                home_html, anchors=parsers.UPCOMING_ANCHORS,
                empty_markers=parsers.UPCOMING_EMPTY_MARKERS,
                item_count=len(items), prev_count=prev_count("upcoming"), error=home_err,
            ),
            {},
        )

    if "posts" in requested:
        items = parsers.parse_recent_posts(home_html, config.BASE_URL, limit=50) if home_html else []
        collected["posts"] = (
            items,
            health.evaluate(
                home_html, anchors=parsers.FEED_ANCHORS,
                empty_markers=parsers.FEED_EMPTY_MARKERS,
                item_count=len(items), prev_count=prev_count("posts"), error=home_err,
            ),
            {},
        )

    if "messages" in requested:
        html, err = await _grab("/messages", wait_selector="table.privatemsg-list")
        items = parsers.parse_messages(html, config.BASE_URL) if html else []
        collected["messages"] = (
            items,
            health.evaluate(
                html, anchors=parsers.MESSAGES_ANCHORS,
                empty_markers=parsers.MESSAGES_EMPTY_MARKERS,
                item_count=len(items), prev_count=prev_count("messages"), error=err,
            ),
            {},
        )

    if "calendar" in requested:
        # The feed is not HTML, so there is no page skeleton to anchor on. A 200
        # response whose body starts with BEGIN:VCALENDAR is the equivalent
        # guarantee, and `client.get_text(expect=...)` already enforced it.
        previous = prev_count("calendar")
        try:
            resolved_ical, events, feed_health = await _load_calendar(
                ical_url, home_html
            )
            cal_health = health.evaluate_feed(
                len(events), previous, extra={"feed": feed_health}
            )
        except Exception as exc:  # noqa: BLE001
            events = []
            cal_health = health.evaluate_feed(0, previous, error=str(exc))
        # Fingerprint the WHOLE feed, never the requested window: a sliding
        # window would change its digest at every midnight and alert daily.
        collected["calendar"] = (events, cal_health, {})
        notes.append(
            "Calendar change detection covers the whole feed; "
            "`calendar_events` below is only the requested window."
        )

    payload_sources: dict[str, Any] = {}
    new_sources: dict[str, Any] = dict(prior_sources)  # untouched sources pass through
    alerts: list[dict] = []

    detailed = set(detailed_sources or ())
    for name in requested:
        if name not in collected:
            continue
        items, source_health, opts = collected[name]
        precision = (
            "digest"
            if name in watch.DIGEST_ONLY_DEFAULT and name not in detailed
            else "item"
        )
        payload, entry, source_alerts = watch.evaluate_source(
            name,
            items=items,
            health=source_health,
            prev_entry=prior_sources.get(name),
            id_key=opts.get("id_key", "id"),
            meta_key=opts.get("meta_key"),
            include_details=include_details,
            max_detail_items=max_detail_items,
            precision=precision,
        )
        payload_sources[name] = payload
        # An unhealthy source returns the caller's previous entry unchanged, so
        # this assignment is what makes "a failed scrape never touches the
        # baseline" literally true.
        if entry is not None:
            new_sources[name] = entry
        alerts.extend(source_alerts)

    statuses = {n: p.get("status") for n, p in payload_sources.items()}
    if all(s in health.REPORTABLE for s in statuses.values()):
        overall = "ok"
    elif any(s in health.REPORTABLE for s in statuses.values()):
        overall = "partial"
    else:
        overall = "error"

    changed_names = sorted(n for n, p in payload_sources.items() if p.get("changed"))

    result: dict[str, Any] = {
        "generated_at": generated_at,
        "status": overall,
        "first_run": not prior_sources,
        "changed": changed_names,
        "sources": payload_sources,
        "alerts": alerts,
        "notes": notes,
    }

    # Quiet runs are the overwhelmingly common case, and on one of those the
    # baseline we would hand back is byte-identical to the one we were given.
    # Returning it anyway costs the caller ~1.2k tokens to read and another
    # ~1.2k to echo back next time, for nothing. Say "unchanged" instead and
    # let them keep what they already have.
    if new_sources == prior_sources:
        result["baseline_unchanged"] = True
        result["baseline_hint"] = "Nothing moved -- keep your stored baseline as is."
    else:
        result["baseline"] = fingerprint.new_baseline(new_sources, generated_at)

    # Likewise the per-source health block: ~300 bytes of diagnostics each is
    # worth having when something is wrong and pure noise when nothing is.
    if not verbose:
        for payload in payload_sources.values():
            source_health = payload.get("health") or {}
            healthy = source_health.get("status") in health.REPORTABLE
            if healthy and not payload.get("changed"):
                # `payload["status"]` already carries this; the diagnostics are
                # only worth their ~300 bytes when something is wrong.
                payload.pop("health", None)
    if resolved_ical:
        result["ical_url"] = resolved_ical
    if "calendar" in collected:
        events = collected["calendar"][0]
        result["calendar_events"] = ical.window(
            events, calendar_days_back, calendar_days_ahead
        )[:_MAX_EVENTS]
    if home_html:
        result["counters"] = parsers.parse_header_counters(home_html)
    return result


@mcp.tool()
async def get_health() -> dict[str, Any]:
    """Is the scraper working? Answers "is it broken, or is the account just empty?".

    Loads one page and reports login state, session cookie lifetime and which
    page-skeleton selectors matched. Call this when `check_updates` reports
    `status: "error"`.
    """
    started = datetime.now()
    html, err = await _grab("/home", extra_wait_ms=2_000)
    elapsed_ms = int((datetime.now() - started).total_seconds() * 1000)

    status = health.evaluate(
        html,
        anchors=parsers.FEED_ANCHORS + parsers.UPCOMING_ANCHORS,
        empty_markers=parsers.FEED_EMPTY_MARKERS,
        item_count=len(parsers.parse_recent_posts(html, config.BASE_URL)) if html else 0,
        error=err,
        elapsed_ms=elapsed_ms,
    )
    return {
        "base_url": config.BASE_URL,
        "logged_in": html is not None and err is None,
        "elapsed_ms": elapsed_ms,
        "health": status,
        "counters": parsers.parse_header_counters(html) if html else None,
        "session": await client.session_info(),
        "keepalive": {
            "enabled": config.KEEPALIVE_ENABLED,
            "seconds": config.KEEPALIVE_SECONDS,
        },
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


if __name__ == "__main__":
    mcp.run()
