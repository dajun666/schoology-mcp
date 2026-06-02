"""Schoology MCP server.

Exposes tools that drive a headless browser to log into PAUSD Schoology (via
the ClassLink portal) and return grades, upcoming assignments and recent posts.

Run directly:  python server.py   (communicates over stdio)
"""

import logging
import re
import sys
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from schoology_mcp import config, parsers
from schoology_mcp.browser import SchoologyClient

_ASSIGNMENT_ID_RE = re.compile(r"/assignment/(\d+)")
_COURSE_ID_RE = re.compile(r"/course/(\d+)")
_PAGE_ID_RE = re.compile(r"/page/(\d+)")


def _course_materials_path(url_or_id: str) -> str:
    """Normalize a full course URL, /course/NNN path, or bare id to a fetch path."""
    s = (url_or_id or "").strip()
    if s.isdigit():
        return f"/course/{s}/materials"
    m = _COURSE_ID_RE.search(s)
    if m:
        return f"/course/{m.group(1)}/materials"
    raise ValueError(f"Not a recognizable Schoology course URL/ID: {url_or_id!r}")


def _assignment_path(url_or_id: str) -> str:
    """Normalize a full URL, /assignment/NNN path, or bare id to a fetch path."""
    s = (url_or_id or "").strip()
    if s.isdigit():
        return f"/assignment/{s}"
    m = _ASSIGNMENT_ID_RE.search(s)
    if m:
        return f"/assignment/{m.group(1)}"
    raise ValueError(
        f"Not a recognizable Schoology assignment URL/ID: {url_or_id!r}"
    )

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
async def get_grades(detailed: bool = False) -> dict:
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
async def get_courses() -> dict:
    """Get the list of courses the student is enrolled in."""
    html = await client.fetch("/courses", wait_selector="li.course-item, div.course-card")
    return {
        "base_url": config.BASE_URL,
        "courses": parsers.parse_courses(html, config.BASE_URL),
    }


@mcp.tool()
async def get_upcoming_assignments(days: int = 14, include_info: bool = False) -> dict:
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
async def get_assignment_info(url_or_id: str) -> dict:
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
) -> dict:
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
async def get_material(url: str) -> dict:
    """Open and read a single Schoology material by URL.

    Accepts any material URL returned by `get_course_materials`. Dispatches
    automatically by URL type:
      - /assignment/NNN        → full assignment details (description, due, attachments)
      - /page/NNN              → page title and body text
      - /materials/gp/NNN      → file/document with download and viewer URLs
      - /materials/link/view/NNN → external link title and resolved URL

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
        return parsers.parse_link_info(html, config.BASE_URL)

    if "/materials/gp/" in path or "/materials/" in path:
        html = await client.fetch(path, wait_selector="h1.page-title, .attachments")
        return parsers.parse_document_info(html, config.BASE_URL)

    raise ValueError(f"Unrecognized Schoology material URL: {url!r}")


@mcp.tool()
async def get_recent_posts(limit: int = 20) -> dict:
    """Get the latest posts/updates from the Schoology activity feed."""
    html = await client.fetch("/home", extra_wait_ms=3_000)
    return {
        "base_url": config.BASE_URL,
        "posts": parsers.parse_recent_posts(html, config.BASE_URL, limit=limit),
    }


if __name__ == "__main__":
    mcp.run()
