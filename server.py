"""Schoology MCP server.

Exposes tools that drive a headless browser to log into PAUSD Schoology (via
the ClassLink portal) and return grades, upcoming assignments and recent posts.

Run directly:  python server.py   (communicates over stdio)
"""

import asyncio
import logging
import re
import sys
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

from typing import Any

from mcp.server.fastmcp import FastMCP

from schoology_mcp import (
    apiparse, campus, config, downloads, gdocs, health, ical, parsers,
    responsive, watch,
)
from schoology_mcp.api import SchoologyAPI
from schoology_mcp.browser import SchoologyClient, relogin_only

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


_FOLDER_QUERY_RE = re.compile(r"[?&]f=(\d+)")


def _course_materials_path(url_or_id: str) -> str:
    return _path_from(url_or_id, _COURSE_ID_RE, "/course/{}/materials", "course")


def _course_folder_id(url_or_id: str) -> str | None:
    """The folder a `?f=<id>` materials URL is scoped to, if it names one.

    `_course_materials_path` keeps only the course id, so this query used to be
    dropped in silence and the caller was handed the whole course while having
    asked for one folder -- a wrong answer that looked like a right one."""
    match = _FOLDER_QUERY_RE.search(url_or_id or "")
    return match.group(1) if match else None


def _assignment_path(url_or_id: str) -> str:
    return _path_from(url_or_id, _ASSIGNMENT_ID_RE, "/assignment/{}", "assignment")

# MCP uses stdout for the protocol -- all logging MUST go to stderr.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

client = SchoologyClient()

# The /v1 path. `relogin_only` is passed rather than imported inside api.py so
# that module never depends on Playwright -- a warm session answers with no
# browser process at all.
api = SchoologyAPI(relogin=relogin_only)

log = logging.getLogger("schoology_mcp.server")


@asynccontextmanager
async def lifespan(_server):
    try:
        yield
    finally:
        await client.close()
        await api.close()


mcp = FastMCP("schoology", lifespan=lifespan)


async def _via_api(api_fn, html_fn):
    """Run the /v1 path, falling back to the HTML scraper per SCHOOLOGY_API_MODE.

    Every result carries `source` ("api" / "html" / "html_fallback"). That is
    not decoration: the two paths do not return identical field sets (the API
    cannot fill `submission`, the scraper cannot see nested folders), so a
    silent fallback that quietly halves the data is worse than a slow answer.
    Whoever reads the output needs to know which one produced it.
    """
    if config.API_MODE == "html":
        return {**await html_fn(), "source": "html"}
    try:
        return {**await api_fn(), "source": "api"}
    except Exception as exc:  # noqa: BLE001 - falling back is the whole point
        if config.API_MODE == "api":
            raise
        log.warning("API path failed (%s); falling back to HTML", exc)
        return {**await html_fn(), "source": "html_fallback", "api_error": str(exc)}


def _binary_client():
    """Whichever client downloads files.

    `downloads.py` is duck-typed on `get_binary`/`get_binaries`, so both objects
    fit and the cache never learns which one it got. httpx is preferred for one
    concrete reason beyond speed: it streams, so an oversized file is refused
    from its Content-Length before any of it crosses the wire, where Playwright
    has already buffered the whole body by the time the size is knowable.
    """
    return client if config.API_MODE == "html" else api


# The roster is read by nearly every API path and changes about twice a year,
# yet it was being refetched per call -- four times inside a single
# get_upcoming_assignments(include_info=True), because each per-item
# get_assignment_info() fetched its own copy. The lock matters as much as the
# TTL: without it, a fan-out of parallel callers all miss the empty cache at
# once and issue the same request N times. Short enough that a dropped or added
# course shows up within the same sitting.
_SECTIONS_TTL_SECONDS = 120.0
_sections_cache: tuple[float, dict] | None = None
_sections_lock = asyncio.Lock()


async def _sections(force: bool = False) -> dict:
    """The roster payload. Needed by nearly every API path, for course titles."""
    global _sections_cache
    async with _sections_lock:
        now = time.monotonic()
        if not force and _sections_cache is not None:
            cached_at, payload = _sections_cache
            if now - cached_at < _SECTIONS_TTL_SECONDS:
                return payload
        payload = await api.get(f"/v1/users/{await api.uid()}/sections")
        _sections_cache = (now, payload)
        return payload


_LOGO_SID_RE = re.compile(r"logo-(\d+)_")


def _gradebook_to_roster(sections: dict) -> dict[str, str]:
    """Map each gradebook `section_id` onto the roster section id.

    The gradebook reports a section id that often is not the roster one, and
    the only place the two are related in the payload is the course logo:
    `.../courselogos/logo-<gradebook_sid>_<hash>.png`. Ugly, but it resolved 4
    of 4 graded courses live, and it is a *fast path* rather than the answer --
    a section with no custom logo has no `profile_url` to read (2 of 11 here),
    so anything it cannot resolve falls back to a filtered call.
    """
    out: dict[str, str] = {}
    for section in apiparse.items(sections, "section"):
        match = _LOGO_SID_RE.search(section.get("profile_url") or "")
        if match and section.get("id"):
            out[match.group(1)] = str(section["id"])
    return out


async def _grades_by_section(sections: dict) -> dict:
    """Gradebook payloads keyed by ROSTER section id.

    One unfiltered call covers the common case. Only gradebook entries the logo
    map cannot place cost a filtered `?section_id=` call, whose attribution is
    unambiguous because it comes from what was asked for.

    Known difference from the HTML gradebook, checked row by row against it:
    this payload omits a section entirely when nothing in it is scored yet,
    where the page still lists that course's ungraded assignment rows. Nothing
    is missed as a result -- when the first score lands the section appears and
    its rows alert as `added` rather than `changed`. In the other direction the
    API was strictly better: it reported an overall grade and an assignment
    score that the page's markup did not yield at all.

    Doing it the other way round -- a filtered call for every section -- is also
    correct and was the first implementation, but it costs 11 requests where
    this costs 1, and the rate limiter turns that into seconds: the API budget
    is roughly 7 requests per 2.5s, so a per-section sweep paced itself into
    3.7s while this returns in well under one.
    """
    uid = await api.uid()
    payload = await api.get(f"/v1/users/{uid}/grades")
    logo = _gradebook_to_roster(sections)

    out: dict[str, dict] = {}
    unplaced = []
    for entry in apiparse.items(payload, "section"):
        gradebook_sid = str(entry.get("section_id"))
        roster_sid = logo.get(gradebook_sid)
        if roster_sid:
            out.setdefault(roster_sid, {"section": []})["section"].append(entry)
        else:
            unplaced.append(gradebook_sid)

    if unplaced:
        # Ask each still-unaccounted-for section directly. Only sections we have
        # not already placed are worth probing.
        candidates = [
            str(s["id"]) for s in apiparse.items(sections, "section")
            if s.get("id") and str(s["id"]) not in out
        ]
        log.info(
            "Gradebook sections %s unplaced by logo map; probing %d sections",
            unplaced, len(candidates),
        )
        results = await api.get_many(
            [f"/v1/users/{uid}/grades?section_id={sid}" for sid in candidates]
        )
        for sid, result in zip(candidates, results):
            if isinstance(result, Exception):
                # One unreadable course must not cost the whole gradebook.
                log.warning("grades for section %s failed: %s", sid, result)
                continue
            if apiparse.items(result, "section"):
                out[sid] = result
    return out


async def _events_payload(days_ahead: int = 14, days_back: int = 0) -> dict:
    """The calendar window from /v1.

    The parameters are `start_date` / `end_date` in YYYY-MM-DD. `start` / `end`
    look like they should work and do not error -- they are interpreted as
    paging offsets, and the reply is a confident `{"event": [], "total": 410}`.
    """
    today = datetime.now().date()
    start = today - timedelta(days=max(0, days_back))
    end = today + timedelta(days=max(0, days_ahead))
    return await api.get(
        f"/v1/users/{await api.uid()}/events"
        f"?start_date={start.isoformat()}&end_date={end.isoformat()}&limit=200"
    )


_user_name_cache: dict[str, str] = {}
# Resolving a name costs a request, and the set of teachers and office staff a
# student hears from is small and stable, so the cache does the real work. The
# cap only bounds a pathological first run.
_MAX_NAME_LOOKUPS = 12


async def _user_names(user_ids) -> dict[str, str]:
    """Resolve author ids to display names, cached for the process lifetime.

    /v1 identifies message senders and post authors by id only. The name is a
    detail field rather than a key -- alerts label a message by its subject --
    so a failed lookup degrades to None rather than failing the source.
    """
    wanted = [u for u in dict.fromkeys(user_ids) if u and u not in _user_name_cache]
    for uid in wanted[:_MAX_NAME_LOOKUPS]:
        try:
            user = await api.get(f"/v1/users/{uid}")
        except Exception as exc:  # noqa: BLE001 - a name is never worth failing over
            log.debug("name lookup for %s failed: %s", uid, exc)
            continue
        name = " ".join(
            part for part in (user.get("name_first"), user.get("name_last")) if part
        )
        if name:
            _user_name_cache[str(uid)] = name
    return dict(_user_name_cache)


async def _grab_json(coro):
    """Await an API call, returning (payload, error) instead of raising.

    Mirrors `_grab` for the HTML path: one failed source must be reported as
    that source's health, never abort the run.
    """
    try:
        return await coro, None
    except Exception as exc:  # noqa: BLE001 - reported as source health
        return None, str(exc)


async def _walk_materials(
    course_id: str, max_depth: int = 4, folder_id: str = "0"
) -> list[dict]:
    """Breadth-first walk of a course's folder tree over /v1.

    Breadth-first, not depth-first, so each level's folders are fetched as one
    batch -- `get_many` is capped at 4 concurrent and paced by the token
    bucket, and a depth-first walk would serialize into one request at a time.

    `max_depth` of 4 covers what real courses use (a real course nests
    root -> Unit -> Resources -> Flashcards). A `seen` set guards the
    walk: a self-referencing folder should be impossible, but one would spend
    the whole rate-limit budget discovering that.

    `folder_id` starts the walk somewhere other than the root, which is what a
    `?f=<id>` URL asks for.
    """
    root = await api.get(f"/v1/courses/{course_id}/folder/{folder_id}")
    materials = apiparse.folder_items(root, course_id, config.BASE_URL)
    out = list(materials)

    seen = {str(folder_id)}
    frontier = [(m["folder_id"], m["title"], [m["title"]])
                for m in materials if m["type"] == "folder" and m["folder_id"]]

    for _ in range(max(0, max_depth - 1)):
        frontier = [(fid, title, path) for fid, title, path in frontier
                    if fid not in seen]
        if not frontier:
            break
        seen.update(fid for fid, _, _ in frontier)
        results = await api.get_many(
            [f"/v1/courses/{course_id}/folder/{fid}" for fid, _, _ in frontier]
        )
        next_frontier = []
        for (fid, title, path), result in zip(frontier, results):
            if isinstance(result, Exception):
                # One unreadable folder must not abort the sweep -- same
                # convention as the HTML path's `fetch_error`.
                log.warning("folder %s failed: %s", fid, result)
                out.append({"type": "folder", "title": title, "folder_id": fid,
                            "url": f"{config.BASE_URL}/course/{course_id}"
                                   f"/materials?f={fid}",
                            "folder": path[-2] if len(path) > 1 else None,
                            "fetch_error": str(result)})
                continue
            children = apiparse.folder_items(
                result, course_id, config.BASE_URL, folder_title=title
            )
            for child in children:
                child["path"] = path
            out.extend(children)
            next_frontier.extend(
                (c["folder_id"], c["title"], path + [c["title"]])
                for c in children if c["type"] == "folder" and c["folder_id"]
            )
        frontier = next_frontier
    return out


# Any id we have seen -> (section id, assignment id). Both an event id and an
# assignment id map to the same entry, because callers arrive holding either.
_assignment_index: dict[str, tuple[str, str | None]] = {}


async def _resolve_assignment(any_id: str) -> tuple[str | None, str | None]:
    """Turn whatever id the caller has into (section_id, assignment_id).

    Two problems at once. `/v1/sections/<sid>/assignments/<aid>` needs a
    section, which neither a bare id nor an `/assignment/<id>` URL carries;
    probing all eleven sections would cost eleven requests for one lookup. And
    **an event id is not an assignment id** -- the sequences do not overlap, so
    an id lifted from a calendar row 403s against the assignments endpoint.

    The school-year event window answers both in one request (the same one
    check_updates already makes), because each event names its section and
    carries the assignment id behind it. Cached for the process. A miss returns
    (None, None) and the caller falls back to the page, which needs neither.
    """
    if any_id in _assignment_index:
        return _assignment_index[any_id]
    year_start, year_end = apiparse.school_year_window()
    payload = await api.get(
        f"/v1/users/{await api.uid()}/events"
        f"?start_date={year_start.isoformat()}"
        f"&end_date={year_end.isoformat()}&limit=1000"
    )
    for event in apiparse.items(payload, "event"):
        sid = event.get("section_id")
        if not sid:
            continue
        aid = event.get("assignment_id")
        entry = (str(sid), str(aid) if aid else None)
        for key in (aid, event.get("id")):
            if key:
                _assignment_index.setdefault(str(key), entry)
    return _assignment_index.get(any_id, (None, None))


async def _grade_items_by_section(section_ids: list[str]) -> dict:
    """Row metadata per section -- the only source of gradebook row titles.

    `grade_items`, deliberately, not `assignments`: it is a superset. A grade
    column a teacher adds by hand ("Course Outline signed") is graded but is not
    an assignment, so `/assignments` leaves it out and those rows came back
    labelled "Assignment <id>". Same response key, so nothing downstream cares.
    """
    results = await api.get_many(
        [f"/v1/sections/{sid}/grade_items?limit=200" for sid in section_ids]
    )
    return {
        sid: result
        for sid, result in zip(section_ids, results)
        if not isinstance(result, Exception)
    }


async def _categories_by_section(section_ids: list[str]) -> dict:
    """Grading categories and their weights, for the gradebook's middle tier.

    The grades payload nests period -> assignment with no category level, but
    the weights ("Tests 50%") are the answer to "why is my grade what it is",
    so the page's three-tier shape is rebuilt from these.
    """
    results = await api.get_many(
        [f"/v1/sections/{sid}/grading_categories" for sid in section_ids]
    )
    return {
        sid: result
        for sid, result in zip(section_ids, results)
        if not isinstance(result, Exception)
    }


@mcp.tool()
async def get_grades(detailed: bool = False) -> dict[str, Any]:
    """Get the student's current grades.

    Default: one entry per course with its overall grade -- small and quick.
    Pass `detailed=True` to also include every grading period, category and
    individual assignment row (the full payload, often hundreds of rows).
    """
    def summarize(courses):
        if detailed:
            return courses
        return [
            {"title": c["title"], "course_grade": c["course_grade"]}
            for c in courses
        ]

    async def via_api():
        sections = await _sections()
        grades_by_sid = await _grades_by_section(sections)
        # Row titles and category weights cost two more calls per graded
        # section, and only the detailed view shows either -- the summary is
        # course name plus overall grade, so it skips both round trips.
        graded_ids = list(grades_by_sid)
        rows_meta = categories = None
        if detailed:
            rows_meta, categories = await asyncio.gather(
                _grade_items_by_section(graded_ids),
                _categories_by_section(graded_ids),
            )
        return {
            "base_url": config.BASE_URL,
            "courses": summarize(
                apiparse.grades(
                    sections, grades_by_sid, rows_meta, categories, config.BASE_URL
                )
            ),
        }

    async def via_html():
        html = await client.fetch(
            "/grades/grades", wait_selector="li.s-grades-course-item"
        )
        return {
            "base_url": config.BASE_URL,
            "courses": summarize(parsers.parse_grades(html, config.BASE_URL)),
        }

    return await _via_api(via_api, via_html)


@mcp.tool()
async def get_courses() -> dict[str, Any]:
    """Get the list of courses the student is enrolled in."""
    async def via_api():
        return {
            "base_url": config.BASE_URL,
            "courses": apiparse.courses(await _sections(), config.BASE_URL),
        }

    async def via_html():
        html = await client.fetch(
            "/courses", wait_selector="li.course-item, div.course-card"
        )
        return {
            "base_url": config.BASE_URL,
            "courses": parsers.parse_courses(html, config.BASE_URL),
        }

    return await _via_api(via_api, via_html)


@mcp.tool()
async def get_upcoming_assignments(days: int = 14, include_info: bool = False) -> dict[str, Any]:
    """Get upcoming / due-soon assignments and events.

    `days` is an advisory window; the home widget already shows near-term
    items, so all of them are returned for the model to filter.

    `include_info=True` additionally fetches each assignment's description /
    attachments (one extra page load per item -- noticeably slower). Off by
    default so the common case stays cheap; turn it on when the model needs
    to read what the assignment is actually asking for.

    On the API path each row also carries `course`, `section_id` and `kind`
    (assignment / assessment / event / grade_column). Work posted as a plain
    calendar event never reached the home widget at all, so those rows are new
    information, not a formatting change.
    """
    async def attach_info(assignments):
        """Fill in each assignment's body, all of them at once.

        Fetched in parallel rather than in sequence: these are independent
        lookups, the client already caps concurrency, and serially they added
        roughly 0.6s per item to a tool that is slow enough already.
        """
        if not include_info:
            return assignments

        async def one(a):
            url = a.get("url")
            if not url or "/assignment/" not in url:
                # /event/... items have different markup -- skip.
                return
            try:
                detail = await get_assignment_info(url)
                a["description"] = detail.get("description")
                a["description_html"] = detail.get("description_html")
                a["attachments"] = detail.get("attachments")
            except Exception as exc:  # noqa: BLE001 - one bad page must not abort the batch
                a["info_error"] = str(exc)

        await asyncio.gather(*(one(a) for a in assignments))
        return assignments

    async def via_api():
        sections, payload = await asyncio.gather(
            _sections(), _events_payload(days_ahead=days)
        )
        rows = apiparse.events(payload, sections, config.BASE_URL)
        # The widget this replaces showed due work, not the whole calendar.
        assignments = [r for r in rows if r["is_assignment"] or r["due_iso"]]
        return {
            "base_url": config.BASE_URL,
            "window_days": days,
            "assignments": await attach_info(assignments),
        }

    async def via_html():
        html = await client.fetch("/home", extra_wait_ms=3_000)
        assignments = parsers.parse_upcoming_assignments(html, config.BASE_URL)
        return {
            "base_url": config.BASE_URL,
            "window_days": days,
            "assignments": await attach_info(assignments),
        }

    return await _via_api(via_api, via_html)


@mcp.tool()
async def get_assignment_info(url_or_id: str) -> dict[str, Any]:
    """Fetch a single Schoology assignment's full details.

    Accepts a full URL, a relative path like `/assignment/123`, or the bare
    numeric assignment id. Returns title, course, due date, description body
    (text + HTML) and any attached files.

    The API path needs a section id, which a bare assignment id does not carry;
    it is looked up from the school-year event index. Anything that index does
    not know falls back to the page, which needs no section at all.
    """
    path = _assignment_path(url_or_id)
    assignment_id = _ASSIGNMENT_ID_RE.search(path).group(1)

    async def via_api():
        section_id, resolved = await _resolve_assignment(assignment_id)
        if not section_id or not resolved:
            raise LookupError(
                f"{assignment_id} is not in the school-year event index (it may "
                "sit outside the year, be unpublished, or have no assignment "
                "behind it)"
            )
        sections, detail = await asyncio.gather(
            _sections(),
            api.get(f"/v1/sections/{section_id}/assignments/{resolved}"),
        )
        info = apiparse.assignment(detail, sections, section_id, config.BASE_URL)
        # The caller may have handed us an event id; report the URL that
        # actually resolves, not the one they asked with.
        info["url"] = f"{config.BASE_URL}/assignment/{resolved}"
        return {**info, **_quiz_note(detail, info)}

    async def via_html():
        html = await client.fetch(
            path, wait_selector=".info-body, #main h1, .page-title"
        )
        info = parsers.parse_assignment_info(html, config.BASE_URL)
        info["url"] = f"{config.BASE_URL}{path}"
        return {**info, **_empty_body_note(info)}

    return await _via_api(via_api, via_html)


def _empty_body_note(info: dict) -> dict:
    """Flag a record that came back with nothing readable in it.

    A page that yields a title and a course but no description, due date or
    attachments is indistinguishable, field for field, from an assignment that
    genuinely has none -- and the two call for opposite responses: report the
    assignment as empty, or go and read it another way. Quizzes are the common
    case (their body lives in the JS quiz player), but so is a page whose
    markup the selectors no longer match, which is exactly the failure this
    flag exists to make visible rather than silent.
    """
    if info.get("description") or info.get("due") or info.get("attachments"):
        return {}
    return {
        "content_available": False,
        "content_note": (
            "Nothing readable came back for this assignment -- no description, "
            "due date or attachments. It may genuinely be empty, be a quiz "
            "whose questions live in the quiz player, or be a page this parser "
            "can no longer read. Open the URL in a browser to tell which."
        ),
    }


@mcp.tool()
async def get_course_materials(
    course_id_or_url: str,
    include_folder_contents: bool = True,
    max_depth: int = 4,
) -> dict[str, Any]:
    """Get the materials list for a Schoology course.

    Accepts a full course URL, a relative path like `/course/123`, or the bare
    numeric course id. Returns all top-level materials (folders, assignments,
    documents, pages, links). With `include_folder_contents=True` (default)
    each folder's contents are fetched too and returned with a `folder` field
    indicating which folder they came from.

    A `?f=<id>` on the URL scopes the walk to that folder rather than the whole
    course; the reply then carries `scope: "folder"` and `folder_id`.

    Each row carries both a `url` (the page a human opens) and an `api_path`
    (the /v1 endpoint that serves it). Pass either to `get_material` -- prefer
    `api_path`, which names the section, because a document's section cannot be
    recovered from its id alone.

    The API path walks the folder tree to `max_depth` levels. The HTML path
    could only ever see one level, which is why an entire unit's classwork
    sitting in `Unit 1 - Intro/Classwork` was invisible to every tool. Rows
    from below the first level carry a `path` listing their ancestor folders.
    """
    course_id = _COURSE_ID_RE.search(_course_materials_path(course_id_or_url)).group(1)
    root_path = _course_materials_path(course_id_or_url)
    folder_id = _course_folder_id(course_id_or_url)
    if folder_id:
        root_path = f"{root_path}?f={folder_id}"

    async def via_api():
        materials = await _walk_materials(
            course_id,
            max_depth=max_depth if include_folder_contents else 1,
            folder_id=folder_id or "0",
        )
        result = {
            "base_url": config.BASE_URL,
            "course_url": f"{config.BASE_URL}{root_path}",
            "max_depth": max_depth,
            "materials": materials,
        }
        if folder_id:
            result["folder_id"] = folder_id
            result["scope"] = "folder"
        return result

    async def via_html():
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
                    for child in parsers.parse_course_materials(
                        folder_html, config.BASE_URL
                    ):
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

    return await _via_api(via_api, via_html)


@mcp.tool()
async def get_material(
    url: str,
    export_google_docs: bool = True,
    allow_binary: bool = False,
) -> dict[str, Any]:
    """Open and read a single Schoology material by URL.

    Accepts any `url` or `api_path` returned by `get_course_materials`.
    Dispatches automatically:
      - documents  → `type` "document" (download_url, filename, filesize) when
                     a file is attached, or "link" (url) when the teacher
                     bookmarked something instead
      - pages      → title and body text
      - discussions→ title and prompt text
      - assignments / quizzes → description, due, attachments

    A quiz comes back with `content_available: false`: both of Schoology's quiz
    engines expose an assignment record with an empty body, because the
    questions live in the quiz player and no endpoint here reaches them.

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

    # Schoology hands out external links through a redirector --
    # `/link?a=&path=<target, percent-encoded>` -- and that is the form that
    # appears in course listings and inside page bodies, so callers arrive here
    # with one directly, not only with the `/materials/link/view/<id>` page that
    # wraps it. The target is already in the query string, so this resolves with
    # no request at all; that is also why it is decided ahead of the /v1 routing
    # below rather than inside either fetch path.
    redirect_target = parsers.link_redirect_target(path)
    if redirect_target is not None:
        return await _material_over_redirect(
            redirect_target, path, export_google_docs, allow_binary
        )

    # The /v1 routes first. `get_course_materials` hands every row an
    # `api_path`, and a course-scoped site URL carries the same two ids, so in
    # both cases the endpoint AND the section that serves it are already known
    # -- a document's section cannot be recovered from its id alone, which is
    # why this has to come from the row rather than be re-derived.
    api_path = path if path.startswith("/v1/") else _material_api_path(path)
    if api_path:
        async def via_api():
            return await _material_over_api(api_path, export_google_docs, allow_binary)

        async def via_html():
            return await _material_over_html(
                _site_path_for(api_path) or path, export_google_docs, allow_binary
            )

        return await _via_api(via_api, via_html)

    return await _material_over_html(path, export_google_docs, allow_binary)


_MATERIALS_URL_RE = re.compile(
    r"/course/(\d+)/materials/(document|page|discussion|assignment|assessment)/(\d+)"
)
# Site leaf -> /v1 collection. Schoology pluralises inconsistently, so the
# mapping is spelled out rather than derived.
_V1_COLLECTION = {"document": "documents", "page": "pages",
                  "discussion": "discussions",
                  "assignment": "assignments", "assessment": "assignments"}
_V1_PATH_RE = re.compile(r"^/v1/sections/(\d+)/(\w+)/(\d+)")
_SITE_LEAF_FOR = {"documents": "document", "pages": "page",
                  "discussions": "discussion", "assignments": "assignment"}


def _material_api_path(path: str) -> str | None:
    """The /v1 endpoint for a course-scoped material URL, or None."""
    match = _MATERIALS_URL_RE.search(path)
    if not match:
        return None
    course_id, leaf, item_id = match.groups()
    return f"/v1/sections/{course_id}/{_V1_COLLECTION[leaf]}/{item_id}"


def _site_path_for(api_path: str) -> str | None:
    """The human-openable page for a /v1 material path, for the HTML fallback."""
    match = _V1_PATH_RE.match(api_path)
    if not match:
        return None
    section_id, collection, item_id = match.groups()
    leaf = _SITE_LEAF_FOR.get(collection)
    if not leaf:
        return None
    if leaf in ("page", "discussion"):
        # Both answer 200 on their short form; the document one does not.
        return f"/{leaf}/{item_id}"
    if leaf == "assignment":
        return f"/assignment/{item_id}"
    return f"/course/{section_id}/materials/{leaf}/{item_id}"


async def _material_over_api(
    api_path: str, export_google_docs: bool, allow_binary: bool
) -> dict[str, Any]:
    """Read one material over /v1, dispatching on the endpoint's collection."""
    match = _V1_PATH_RE.match(api_path)
    collection = match.group(2) if match else ""
    section_id = match.group(1) if match else None

    payload, sections = await asyncio.gather(api.get(api_path), _sections())
    course = apiparse.course_title(sections, section_id) if section_id else None

    if collection == "pages":
        return apiparse.page(payload, course=course)
    if collection == "discussions":
        return apiparse.discussion(payload, config.BASE_URL, course=course)
    if collection == "assignments":
        info = apiparse.assignment(payload, sections, section_id, config.BASE_URL)
        info["url"] = f"{config.BASE_URL}/assignment/{payload.get('id')}"
        return {**info, **_quiz_note(payload, info)}

    info = apiparse.document(payload, config.BASE_URL, course=course)
    # A "document" that is really a bookmark is frequently a Google Doc, and
    # the reader gets nothing from the link alone -- same reasoning as the
    # /materials/link/view/ branch below, which is the HTML form of this row.
    if (info.get("type") == "link" and export_google_docs
            and gdocs.is_drive_url(info.get("url"))):
        info["google_doc"] = await gdocs.fetch_document(
            info["url"], allow_binary=allow_binary
        )
    return info


def _quiz_note(payload: dict, info: dict) -> dict:
    """Say so when an assessment's questions are simply not in the API.

    Both quiz engines return an assignment record with an empty `description`
    and no attachments: the questions live in Schoology's JavaScript quiz app
    and no endpoint here exposes them. Verified 2026-09-02 on three practice
    quizzes across both engines. Reporting the empty record as-is reads
    identically to a quiz that has no instructions, so the distinction is made
    explicit rather than left to the caller to guess.
    """
    kind = str(payload.get("type") or "")
    if not kind.startswith("assessment"):
        return {}
    if info.get("description") or info.get("attachments"):
        return {}
    return {
        "content_available": False,
        "content_note": (
            "This is a Schoology quiz; its questions live in the quiz player "
            "and are not exposed by any API or page endpoint. Open it in a "
            "browser to see them."
        ),
    }


async def _material_over_redirect(
    target: str, path: str, export_google_docs: bool, allow_binary: bool
) -> dict[str, Any]:
    """A `/link?path=<target>` redirector, resolved without a request.

    Shaped like `parsers.parse_link_info` so a caller cannot tell which of the
    two produced the row, minus `title` and `course`: those live on the wrapping
    material page and are not worth a fetch to recover, since the reader asked
    for the target, not for the wrapper.

    A target pointing back into Schoology is returned as a link rather than
    followed -- one hop is what the redirector means, and re-entering the
    dispatcher on a value the site controls is how a loop gets built.
    """
    if not target:
        raise ValueError(f"Schoology link URL carries no target: {path!r}")
    info = {"type": "link", "title": None, "course": None, "url": target}
    if export_google_docs and gdocs.is_drive_url(target):
        info["google_doc"] = await gdocs.fetch_document(
            target, allow_binary=allow_binary
        )
    return info


async def _material_over_html(
    path: str, export_google_docs: bool, allow_binary: bool
) -> dict[str, Any]:
    """The original page-scraping dispatcher, now also the API path's fallback."""
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

    if "/discussion/" in path:
        html = await client.fetch(path, wait_selector="h1.page-title, .s-rte")
        return parsers.parse_page_content(html, config.BASE_URL)

    raise ValueError(f"Unrecognized Schoology material URL: {path!r}")


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

    results = await downloads.fetch_many(
        _binary_client(), [i["url"] for i in images], max_bytes
    )
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

    Deliberately still on the HTML path. `/v1/recent` is 19x faster and even
    returns the full post body where the feed markup truncates it, but it
    carries no image URLs -- and notices here are routinely posted as a picture
    with no text at all, which is the entire reason this tool downloads images.
    A faster tool that renders those posts blank is not a faster tool.

    `check_updates` does use the API for its posts source: detecting that a
    post appeared or changed needs no pictures.
    """
    html = await client.fetch(
        "/home",
        # The feed is JS-rendered, so something has to be waited for -- but a
        # flat 3s sleep pays the worst case on every call. Waiting for the
        # first feed item instead cut this from 4.63s to ~2s with the same 8
        # posts parsed.
        #
        # The settle afterwards is deliberately not zero, and not the 500ms the
        # timings alone would justify: the sample that proved the selector had
        # **no image posts in it**, and `_extract_post_images` reads only
        # `src`, so if Schoology lazy-loads feed images the selector could
        # match before any `src` is populated and the pictures would be dropped
        # in silence. Notices here are frequently a picture with no text at
        # all, so that failure is worse than the second it buys back. Drop this
        # to 500 once a feed containing an image post has been measured.
        wait_selector=parsers.FEED_ITEM_SELECTOR,
        extra_wait_ms=1_000,
    )
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

    Deliberately still on the HTML path while the rest moved to /v1. The API's
    `/v1/messages/inbox` is far faster (0.12s against 3.45s) but returns
    `message: null` for every row and identifies the sender only by
    `author_id` -- no preview text and no name. The preview is most of what
    this tool is *for*, and the only way to obtain one over the API is to fetch
    the thread, which is exactly the side effect the tool promises not to
    cause. Speed is not worth marking a student's mail read.

    `check_updates` does use the API for its messages source: detecting that a
    new thread arrived needs only id, subject and timestamp, none of which
    require opening anything.
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
# (several hundred, going back years). Never hand a model the whole thing.
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

    On the API path every row carries `section_id`, `course` and `kind`, which
    settles the question the iCal feed could never answer: which class an event
    belongs to. Titles are actively misleading about this -- one course's
    "Widget Contract Due" reads exactly like another's integrity contract --
    and the old workaround was to fetch `/event/<id>/profile` and dig
    `realm_id` out of the markup. It is now a field.

    The API window also includes assignment- and assessment-typed rows, which
    the iCal feed omits: at PAUSD that feed carries school events only. Filter
    on `kind` if you want the old behaviour.
    """
    async def via_api():
        sections, payload = await asyncio.gather(
            _sections(), _events_payload(days_ahead=days_ahead, days_back=days_back)
        )
        rows = apiparse.events(payload, sections, config.BASE_URL)
        if query:
            needle = query.lower()
            rows = [
                r for r in rows
                if needle in (r.get("summary") or "").lower()
                or needle in (r.get("description") or "").lower()
            ]
        return {
            "base_url": config.BASE_URL,
            "ical_url": None,
            "window": {"days_back": days_back, "days_ahead": days_ahead},
            "query": query,
            "health": {"status": "ok", "in_window": len(rows)},
            "truncated": len(rows) > _MAX_EVENTS,
            "events": rows[:_MAX_EVENTS],
        }

    async def via_html():
        url, events, feed_health = await _load_calendar(ical_url)
        selected = ical.search(ical.window(events, days_back, days_ahead), query)
        return {
            "base_url": config.BASE_URL,
            "ical_url": url,
            "window": {"days_back": days_back, "days_ahead": days_ahead},
            "query": query,
            "health": {**feed_health, "in_window": len(selected)},
            "truncated": len(selected) > _MAX_EVENTS,
            "events": selected[:_MAX_EVENTS],
        }

    return await _via_api(via_api, via_html)


@mcp.tool()
async def download_file(url: str, max_mb: float = 25.0) -> dict[str, Any]:
    """Download a file linked from Schoology and say where it landed.

    Give it any link you found in a material, assignment description or message:

      - Google Docs/Sheets/Slides → exported (Docs come back as Markdown text in
        `content`, with embedded images written out as openable files)
      - Uploaded Drive files (PDF, image, video) → downloaded as-is, path returned
      - Schoology-hosted attachments → downloaded with the logged-in session

    Anything over `max_mb` is declined with its real size reported — a real
    assignment linked a 110 MB video. Nothing over the limit is transferred:
    Drive is checked by a metadata-only probe, and Schoology attachments stream,
    so an oversized one is refused from its Content-Length and a body that
    understates its length is abandoned partway through. (On the legacy
    `SCHOOLOGY_API_MODE=html` path the response is already buffered by the time
    its size is known, so there the limit only keeps it out of the cache.)
    Raise `max_mb` if you want the file.

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
        downloader = _binary_client()
        result = await downloads.fetch_to_cache(downloader, target, max_bytes)
        # `source` here means where the file came from (drive / schoology), not
        # which client fetched it -- that is `fetched_via`. Two different
        # questions that would otherwise collide on one key.
        return {
            "source": "schoology",
            "fetched_via": "api" if downloader is api else "browser",
            "url": target,
            **result,
        }

    return {
        "url": url,
        "error": (
            "Only Google Drive and Schoology URLs are supported. This tool "
            "exists to fetch things behind the school login, not to browse the "
            "web."
        ),
    }


# --------------------------------------------------------------------------
# Infinite Campus (optional -- CAMPUS_ENABLED, off by default)
# --------------------------------------------------------------------------
#
# Registered conditionally rather than always-present-but-erroring: an unusable
# tool still costs its schema in every request, and this one is district-
# specific. When it is off the server looks exactly as it did before.

if config.CAMPUS_ENABLED:

    @mcp.tool()
    async def get_schedule(
        term: str | None = None,
        schedule: str | None = campus.DEFAULT_SCHEDULE,
    ) -> dict[str, Any]:
        """Get the class schedule from Infinite Campus: period, time, ROOM number.

        Schoology knows the coursework; the district SIS knows where and when
        the class actually meets. Use this for "what room is my next class in",
        "what period is a given course", or the day's running order.

        One row per class per term, ordered by period. `term` ("S1"/"S2") picks
        a semester -- a year-long course sits in both, so without it every class
        appears twice.

        `schedule` selects the bell schedule: "Full" (default) is the regular
        day; "M"/"T"/"W"/"R"/"F" are day-specific variants where times differ
        (a block day can move a class by hours). Pass None to see every
        placement. `schedules_available` lists what this district uses.
        """
        data = await client.campus_json(campus.ROSTER_PATH)
        rows = campus.parse_roster(data, term=term, schedule=schedule)
        return {
            "base_url": config.CAMPUS_BASE_URL,
            "term": term,
            "schedule_used": schedule,
            "terms_available": campus.terms_in(campus.parse_roster(data)),
            "schedules_available": campus.schedules_in(data),
            "count": len(rows),
            "schedule": rows,
        }

    async def _responsive_sessions(include_empty: bool = False):
        """(enrolment ids, parsed sessions) -- the read both tools start from."""
        roster = await client.campus_json(campus.ROSTER_PATH)
        ids = responsive.enrollment(roster)
        if ids is None:
            return None, []
        data = await client.campus_json(
            responsive.SCHEDULE_PATH + responsive.query(ids)
        )
        return ids, responsive.parse_sessions(data, include_empty=include_empty)

    @mcp.tool()
    async def get_responsive_schedule(include_empty: bool = False) -> dict[str, Any]:
        """List the flex-period sessions and the offerings open in each.

        The flex period -- a school names it whatever it likes (Flex, Tutorial,
        Advisory) -- is scheduled per day: each session offers a list of
        teachers' rooms with seat caps, and the student holds one of them. Use
        this to see what is on offer for an upcoming day, how many seats are
        left in each, and which one is currently held.

        Per session: `open` (still accepting changes), `changeable` (a
        selection may be made -- false when staff placed the student or there
        is nothing to choose between), `selected_offering_id`, and `offerings`
        sorted by name. Per offering: teacher, room, `seats_left`, `full`,
        `selected`, and `selectable` -- true exactly when
        `set_responsive_schedule` would accept it.

        Sessions the school has published with no offerings yet are hidden, as
        they are in the portal; `include_empty=True` shows them.
        """
        ids, sessions = await _responsive_sessions(include_empty)
        return {
            "base_url": config.CAMPUS_BASE_URL,
            "enrolled": ids is not None,
            "count": len(sessions),
            "sessions": sessions,
        }

    @mcp.tool()
    async def set_responsive_schedule(
        offering_id: int,
        session_id: int | None = None,
    ) -> dict[str, Any]:
        """Claim a seat in one flex-period offering, replacing the one held.

        `offering_id` is an `offering_id` from `get_responsive_schedule`.
        `session_id` disambiguates when several sessions are listed; omitted,
        the earliest one is used.

        This writes to the SIS and is visible to the school -- the student's
        attendance for that period is taken in the room this picks. It is
        reversible while the session is `open`: calling it again moves the
        seat. It cannot free a seat without taking another.

        Eligibility is checked against a fresh read first, so an ineligible
        request returns `{"changed": false, "refused": <reason>}` without a
        write. Reasons: `offering_not_in_session`, `already_selected`,
        `session_closed`, `placed_by_staff` (a teacher or administrator chose
        for the student), `offering_full`, `session_not_changeable`.
        """
        ids, sessions = await _responsive_sessions()
        if ids is None:
            return {"changed": False, "refused": "no_enrollment"}

        session = responsive.find_session(sessions, session_id)
        if session is None:
            return {"changed": False, "refused": "session_not_found",
                    "sessions": [{"session_id": s["session_id"], "name": s["name"]}
                                 for s in sessions]}

        offering = responsive.find_offering(session, offering_id)
        refused = responsive.refusal(session, offering)
        if refused:
            return {"changed": False, "refused": refused,
                    "session": {k: session[k] for k in
                                ("session_id", "name", "date", "open",
                                 "changeable", "selected_offering_id")},
                    "offering": offering}

        was = session["selected"]
        result = await client.campus_post_json(
            responsive.UPDATE_PATH,
            responsive.payload(ids, session["session_id"], offering["offering_id"]),
        )
        ok = bool(isinstance(result, dict) and result.get("success"))
        return {
            "changed": ok,
            "session": {k: session[k] for k in ("session_id", "name", "date")},
            "was": was,
            "now": offering if ok else was,
            "message": (result or {}).get("message") if isinstance(result, dict) else None,
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


async def _collect_api(requested, prev_count, days_ahead, days_back):
    """Fetch every requested source from /v1, concurrently.

    Five sources in one fan-out. The whole set measured 0.39s against roughly
    13s of serialized page renders, and the token bucket keeps the burst inside
    the rate limit without anyone here thinking about it.

    Each source is judged with `health.evaluate_feed`: there is no page
    skeleton to anchor on, and a 200 carrying well-formed JSON is the
    equivalent guarantee, which `api.get` already enforced. The distinction
    that matters is preserved -- an empty list is genuinely empty, while an
    exception is an error, and only the latter suppresses alerts and freezes
    the caller's baseline.
    """
    wanted = set(requested)
    needs_sections = bool({"grades", "upcoming", "calendar"} & wanted)

    jobs = {}
    if needs_sections:
        jobs["sections"] = _grab_json(_sections())
    if {"upcoming", "calendar"} & wanted:
        # One wide, stable fetch serves both sources; the caller's window is
        # applied afterwards by filtering, so it costs no extra request.
        year_start, year_end = apiparse.school_year_window()
        jobs["events"] = _grab_json(api.get(
            f"/v1/users/{await api.uid()}/events"
            f"?start_date={year_start.isoformat()}"
            f"&end_date={year_end.isoformat()}&limit=1000"
        ))
    if "messages" in wanted:
        jobs["messages"] = _grab_json(api.get("/v1/messages/inbox?limit=50"))
    if "posts" in wanted:
        jobs["posts"] = _grab_json(api.get("/v1/recent?limit=50"))

    done = dict(zip(jobs, await asyncio.gather(*jobs.values())))
    sections, sections_err = done.get("sections", (None, None))
    notes = []

    collected = {}

    if "grades" in wanted:
        rows, err = [], sections_err
        if sections is not None:
            try:
                grades_by_sid = await _grades_by_section(sections)
                # Titles yes, categories no. Alert labels need the row title,
                # but category rows are pure structure -- they never change, so
                # fetching them here is four requests spent on nothing.
                meta = await _grade_items_by_section(list(grades_by_sid))
                rows = watch.flatten_grades(
                    apiparse.grades(sections, grades_by_sid, meta, None,
                                    config.BASE_URL)
                )
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
        collected["grades"] = (
            rows,
            health.evaluate_feed(len(rows), prev_count("grades"), error=err),
            {"id_key": "uid", "meta_key": "fp_meta"},
        )

    events_payload, events_err = done.get("events", (None, None))
    event_rows = (
        apiparse.events(events_payload, sections, config.BASE_URL)
        if events_payload is not None else []
    )

    if "upcoming" in wanted:
        # "Upcoming" IS a moving window by definition, and that is fine: items
        # leave it by expiring, and `removed` never alerts outside grades.
        today = datetime.now().date()
        lo = (today - timedelta(days=max(0, days_back))).isoformat()
        hi = (today + timedelta(days=max(0, days_ahead))).isoformat()
        due = [
            r for r in event_rows
            if (r["is_assignment"] or r["due_iso"])
            and r["due_iso"] and lo <= r["due_iso"][:10] <= hi
        ]
        collected["upcoming"] = (
            due,
            health.evaluate_feed(len(due), prev_count("upcoming"),
                                 error=events_err or sections_err),
            {},
        )

    if "calendar" in wanted:
        collected["calendar"] = (
            event_rows,
            health.evaluate_feed(len(event_rows), prev_count("calendar"),
                                 error=events_err),
            {},
        )
        # The iCal path had to hash the whole feed because its window slid with
        # the date and would have alerted every midnight. Here the window is an
        # explicit parameter, so it is stable between runs at the same settings
        # -- but changing `calendar_days_ahead` between runs does move the set,
        # and that reads as items added or removed.
        notes.append(
            "Calendar change detection covers the whole school year, not the "
            "requested window: a window anchored to today shifts every "
            "midnight and would alert daily. `calendar_events` below is the "
            "requested window."
        )

    messages_payload, messages_err = done.get("messages", (None, None))
    posts_payload, posts_err = done.get("posts", (None, None))

    # Names are NOT resolved here. Each costs a request, and a quiet run --
    # which the code elsewhere rightly calls the overwhelmingly common case --
    # needs none of them. They are filled in after the diff, for the handful of
    # items that actually produced an alert. Resolving all of them up front
    # spent six seconds per run on a detail field.
    names = {}

    if "messages" in wanted:
        rows = apiparse.messages(messages_payload or {}, names)
        collected["messages"] = (
            rows,
            health.evaluate_feed(len(rows), prev_count("messages"),
                                 error=messages_err),
            {},
        )
        notes.append(
            "Message previews are not available over the API (fetching one "
            "would mark it read); use get_messages for those."
        )

    if "posts" in wanted:
        rows = apiparse.posts(posts_payload or {}, names, config.BASE_URL)
        collected["posts"] = (
            rows,
            health.evaluate_feed(len(rows), prev_count("posts"), error=posts_err),
            {},
        )

    # The HTML path read the unread badge off the /home header. Here it is just
    # a count over the inbox rows, with no extra request.
    extras = {}
    if messages_payload is not None:
        extras["counters"] = {
            "unread_messages": sum(
                1 for m in apiparse.messages(messages_payload) if m["unread"]
            )
        }
    return collected, notes, extras


async def _collect_html(requested, prev_count, ical_url):
    """The original scraper collection, kept intact as the fallback path."""
    collected = {}
    notes = []
    resolved_ical = ical_url

    home_html = home_err = None
    if {"upcoming", "posts"} & set(requested):
        # The activity feed is JS-rendered; the upcoming widget is in the
        # server HTML, so a run without `posts` (the scheduled default) waits
        # for neither. When posts ARE wanted, wait for the first feed item
        # rather than sleeping a flat 3s -- same reasoning and same 1s settle
        # as `get_recent_posts`.
        want_feed = "posts" in requested
        home_html, home_err = await _grab(
            "/home",
            wait_selector=parsers.FEED_ITEM_SELECTOR if want_feed else None,
            extra_wait_ms=1_000 if want_feed else 0,
        )

    if "grades" in requested:
        html, err = await _grab("/grades/grades",
                                wait_selector="li.s-grades-course-item")
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
        items = (parsers.parse_upcoming_assignments(home_html, config.BASE_URL)
                 if home_html else [])
        collected["upcoming"] = (
            items,
            health.evaluate(
                home_html, anchors=parsers.UPCOMING_ANCHORS,
                empty_markers=parsers.UPCOMING_EMPTY_MARKERS,
                item_count=len(items), prev_count=prev_count("upcoming"),
                error=home_err,
            ),
            {},
        )

    if "posts" in requested:
        items = (parsers.parse_recent_posts(home_html, config.BASE_URL, limit=50)
                 if home_html else [])
        collected["posts"] = (
            items,
            health.evaluate(
                home_html, anchors=parsers.FEED_ANCHORS,
                empty_markers=parsers.FEED_EMPTY_MARKERS,
                item_count=len(items), prev_count=prev_count("posts"),
                error=home_err,
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

    extras = {"ical_url": resolved_ical}
    if home_html:
        extras["counters"] = parsers.parse_header_counters(home_html)
    return collected, notes, extras


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

    extras: dict[str, Any] = {}
    if config.API_MODE == "html":
        collected, notes, extras = await _collect_html(
            requested, prev_count, ical_url
        )
    else:
        try:
            collected, notes, extras = await _collect_api(
                requested, prev_count, calendar_days_ahead, calendar_days_back
            )
        except Exception as exc:  # noqa: BLE001
            if config.API_MODE == "api":
                raise
            log.warning("API collection failed (%s); falling back to HTML", exc)
            collected, notes, extras = await _collect_html(
                requested, prev_count, ical_url
            )
            notes.append(f"Fell back to the HTML scraper: {exc}")
    resolved_ical = extras.get("ical_url") or ical_url

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

    # Only now, with the diff done, is it worth spending a request on a name:
    # alerts are few and a quiet run needs none at all. `_user_names` caches
    # process-wide, so a repeatedly-alerting sender is resolved once.
    if alerts and config.API_MODE != "html":
        author_ids = [
            a.get("author_id") for a in alerts if a.get("author_id")
        ] + [
            item.get("author_id")
            for payload in payload_sources.values()
            for group in (payload.get("details") or {}).values()
            if isinstance(group, list)
            for item in group
            if isinstance(item, dict) and item.get("author_id")
        ]
        if author_ids:
            names = await _user_names(author_ids)
            for payload in payload_sources.values():
                for group in (payload.get("details") or {}).values():
                    if not isinstance(group, list):
                        continue
                    for item in group:
                        if isinstance(item, dict) and item.get("author_id"):
                            item.setdefault(
                                "sender", names.get(str(item["author_id"]))
                            )

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
    if extras.get("counters"):
        result["counters"] = extras["counters"]
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
