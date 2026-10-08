"""Pure transforms from /v1 JSON into the shapes the HTML parsers produced.

The counterpart to `parsers.py`: same output contract, different input. Nothing
here does I/O, so the whole module is exercisable from `tests/fixtures/api/`.

**The output shapes are a contract, not an implementation detail.**
`fingerprint.py`, `watch.py`, `scripts/watch_once.py` and the school skills all
index into these dicts by key name. A renamed or dropped key does not raise --
it silently stops an alert from ever firing. When you change one, update
`scripts/check_watch.py::check_apiparse_shapes` in the same commit.

Two traps this module exists to absorb, both verified live 2026-09-01:

* **Gradebook section ids are not roster section ids.** `/v1/users/<uid>/grades`
  reports a `section_id` that, for half the graded courses measured, matches
  nothing in `/v1/users/<uid>/sections` -- the two ids differ in their last
  digits, and the gradebook one appears nowhere in the section object except
  buried inside a CDN course-logo filename. Joining on it yields untitled
  courses. The fix is not a mapping table: call
  `/v1/users/<uid>/grades?section_id=<id>` **per section** (11 concurrent calls,
  0.62s measured) so attribution comes from what was *asked for* rather than
  what came back. `grades()` therefore takes `grades_by_sid` keyed by the roster
  id, and never reads `section_id` out of the payload.
* **Assignment titles are not in the grades payload** -- only `assignment_id`.
  They come from `/v1/sections/<sid>/grade_items`, passed in as
  `assignments_by_sid`. Use `grade_items`, NOT `assignments`: the latter omits
  teacher-created grade columns that carry no submission, and those rows then
  render as "Assignment <id>". Missing titles degrade to that placeholder
  rather than dropping the row -- an unlabelled grade change is still worth an
  alert. Category names and weights come from
  `/v1/sections/<sid>/grading_categories` in `categories_by_sid`; without them
  the item rows still appear, just flat under their grading period.

One deliberate loss against the HTML path: `submission` is always "unknown".
The gradebook page derives it from row icons; the API carries no bulk
equivalent (`/v1/sections/<sid>/submissions/<aid>` is one call per assignment).
It stays in the output because `fp` hashes it and callers read it, but it no
longer varies -- which mostly removes an alert about the student's own
submission, something they already know they did.
"""

import datetime as _dt
import html as _html
import re as _re

from .fingerprint import fp


def _s(value) -> str | None:
    """Normalize a JSON scalar to a non-empty string, or None.

    Schoology is inconsistent about types across endpoints -- section ids arrive
    as strings, assignment ids as integers, `max_points` as either -- so every
    id used as a dict key or in a fingerprint goes through here first.

    Titles also come back **HTML-escaped**, which is easy to miss because most
    of them contain nothing to escape: a course called "Widgets &amp; Sprockets
    ..." was the only row in a 28-item tree that gave it away, and it would
    otherwise have been saved into notes and fingerprints in that form.
    """
    if value is None:
        return None
    text = _html.unescape(str(value)).strip()
    return text or None


# --------------------------------------------------------------------------
# Courses  ->  parsers.parse_courses shape: {"title", "url"}
# --------------------------------------------------------------------------

def courses(sections_payload, base_url):
    """`/v1/users/<uid>/sections` -> the list `get_courses` returns.

    The HTML card's text was course title, course code and section title run
    together; the API hands those back as separate fields, so the same string
    is rebuilt rather than inventing a new one -- callers and saved notes match
    on these titles.
    """
    out = []
    for section in items(sections_payload, "section"):
        sid = _s(section.get("id"))
        parts = [
            _s(section.get("course_title")),
            _s(section.get("course_code")),
            _s(section.get("section_title")),
        ]
        title = " ".join(p for p in parts if p)
        out.append({
            "title": title or None,
            "url": f"{base_url}/course/{sid}" if sid else None,
        })
    return out


# --------------------------------------------------------------------------
# Grades  ->  parsers.parse_grades shape
# --------------------------------------------------------------------------

def _grade_cell(raw_grade, max_points=None):
    """Build the `{raw, numeric, max}` dict the gradebook parser emitted.

    Returns None for an ungraded item, matching the HTML path's empty cell --
    rendering an ungraded assignment as 0 would be a lie the alerting layer
    cannot distinguish from a real zero.
    """
    if raw_grade is None:
        return None
    numeric = _s(raw_grade)
    top = _s(max_points)
    data = {"raw": f"{numeric} / {top}" if top else f"{numeric}", "numeric": numeric}
    if top:
        data["max"] = top
    return data


def _final_grade(entry):
    """The section- or period-level score out of a `final_grade` list."""
    finals = entry.get("final_grade") if isinstance(entry, dict) else None
    if not isinstance(finals, list) or not finals:
        return None, None
    first = finals[0] if isinstance(finals[0], dict) else {}
    grade = first.get("grade")
    weight = first.get("weight")
    raw = None if grade is None else {"raw": f"{_s(grade)}%", "numeric": _s(grade)}
    return raw, (f"({_s(weight)}%)" if weight is not None else None)


def grades(sections_payload, grades_by_sid, assignments_by_sid=None,
           categories_by_sid=None, base_url=""):
    """Build the course/period/item tree `get_grades(detailed=True)` returns.

    `grades_by_sid` maps a **roster** section id to that section's
    `/v1/users/<uid>/grades?section_id=<id>` payload -- see the module docstring
    for why attribution cannot come from the payload itself.

    **Every enrolled section is listed, graded or not.** The gradebook page
    shows all eleven of this student's courses with "N/A" against the seven
    that carry no score, and dropping those would quietly answer a different
    question than the HTML path did -- "which courses have grades" instead of
    "what are my grades". An ungraded course comes back with
    `course_grade: None` and a single course row.
    """
    assignments_by_sid = assignments_by_sid or {}
    categories_by_sid = categories_by_sid or {}
    out = []

    for section in items(sections_payload, "section"):
        sid = _s(section.get("id"))
        if sid is None:
            continue
        graded = items(grades_by_sid.get(sid), "section")

        # ": " between course and section, matching the gradebook page's own
        # heading, so a course title saved from either path is the same string.
        course_title = ": ".join(
            p for p in (_s(section.get("course_title")), _s(section.get("section_title"))) if p
        ) or sid
        # `/v1/sections/<sid>/grade_items` rather than `/assignments`: it is a
        # superset. A teacher-created grade column with no submission ("Course
        # Outline signed") is graded but is not an assignment, so /assignments
        # omits it and those rows came back titled "Assignment <id>".
        titles = {
            _s(a.get("id")): a
            for a in items(assignments_by_sid.get(sid), "assignment")
            if _s(a.get("id"))
        }
        cats = {
            _s(c.get("id")): c
            for c in items(categories_by_sid.get(sid), "grading_category")
            if _s(c.get("id"))
        }

        rows = []
        # A section can carry more than one gradebook entry (year-long courses
        # are placed in both terms); flatten them under one course.
        course_grade_cell = None
        for entry in graded:
            entry_grade, _ = _final_grade(entry)
            course_grade_cell = course_grade_cell or entry_grade

            # A section can carry more than one gradebook entry, and period ids
            # repeat across them (one term carries one id everywhere), so
            # the period row id is qualified by the entry's OWN section_id --
            # the gradebook-internal id, which is stable per run even though it
            # does not match the roster id. Without this, two entries collapse
            # onto one uid and `snapshot` silently drops a row from every
            # future diff. Assignment ids are globally unique, so item rows
            # need no such qualification.
            entry_sid = _s(entry.get("section_id"))
            for period in items(entry, "period"):
                raw_pid = _s(period.get("period_id"))
                pid = f"{entry_sid}.{raw_pid}" if entry_sid and len(graded) > 1 else raw_pid
                period_grade, period_weight = _final_grade(period)
                rows.append(_row(
                    row_id=pid, section_id=sid, parent_id=sid, row_type="period",
                    title=_s(period.get("period_title")),
                    percentage_contrib=period_weight, due=None,
                    grade=period_grade, comment=None, assignment_url=None,
                ))

                # Items are grouped under their grading category, the way the
                # gradebook page nests them -- that grouping is where the
                # weights live ("Tests 50%"), and it is the whole answer to
                # "why is my grade what it is".
                buckets: dict[str | None, list] = {}
                for item in items(period, "assignment"):
                    aid = _s(item.get("assignment_id"))
                    meta = titles.get(aid, {})
                    buckets.setdefault(_s(meta.get("grading_category")), []).append(
                        (aid, meta, item)
                    )

                for cat_id, bucket in buckets.items():
                    parent = pid
                    category = cats.get(cat_id)
                    if category is not None:
                        # The same category id recurs in every grading period,
                        # so the row id is qualified by the period.
                        cat_row_id = f"{pid}.{cat_id}"
                        weight = _s(category.get("weight"))
                        rows.append(_row(
                            row_id=cat_row_id, section_id=sid, parent_id=pid,
                            row_type="category", title=_s(category.get("title")),
                            percentage_contrib=f"({weight}%)" if weight else None,
                            # The API reports no per-category score; only the
                            # weight. None, rather than a computed guess.
                            due=None, grade=None, comment=None,
                            assignment_url=None,
                        ))
                        parent = cat_row_id

                    for aid, meta, item in bucket:
                        rows.append(_row(
                            row_id=aid, section_id=sid, parent_id=parent,
                            row_type="item",
                            title=_s(meta.get("title")) or f"Assignment {aid}",
                            percentage_contrib=None,
                            due=_s(meta.get("due")),
                            grade=_grade_cell(item.get("grade"), item.get("max_points")),
                            comment=_s(item.get("comment")),
                            assignment_url=(
                                f"{base_url}/assignment/{aid}" if aid and base_url else None
                            ),
                        ))

        # The course row goes first so the tree reads root-down, as the HTML did.
        rows.insert(0, _row(
            row_id=sid, section_id=sid, parent_id=None, row_type="course",
            title="Course Grade", percentage_contrib=None, due=None,
            grade=course_grade_cell, comment=None, assignment_url=None,
        ))

        out.append({
            "id": sid,
            "title": course_title,
            "course_grade": (course_grade_cell or {}).get("raw"),
            "rows": rows,
            "fp": fp(sid, (course_grade_cell or {}).get("raw")),
        })
    return out


def _row(row_id, section_id, parent_id, row_type, title, percentage_contrib,
         due, grade, comment, assignment_url):
    """One gradebook row, fingerprinted exactly as `parsers.parse_grades` does.

    `uid` stays course-qualified: period ids repeat across courses (and the
    ungraded bucket is literally "0"), so a bare row id is not unique in a flat
    snapshot. `submission` is a fixed "unknown" -- see the module docstring.

    `fp` deliberately excludes the grades payload's `timestamp`: it is the
    moment a score was entered, so a teacher correcting a typo would refresh it
    and alert every run. Grade, comment and submission are the alert-worthy
    fields; title, due and weight live in `fp_meta` as noise.
    """
    submission = "unknown"
    uid = f"{section_id}:{row_id}"
    return {
        "id": row_id,
        "uid": uid,
        "parent_id": parent_id,
        "type": row_type,
        "title": title,
        "percentage_contrib": percentage_contrib,
        "due": due,
        "grade": grade,
        "comment": comment,
        "submission": submission,
        "assignment_url": assignment_url,
        "fp": fp(uid, grade, comment, submission),
        "fp_meta": fp(uid, title, due, percentage_contrib, parent_id),
    }


def items(payload, key):
    """The list under `key`, tolerating a missing key or a single bare object.

    Public because `server.py` needs it to walk raw payloads before they reach
    a transform.

    Schoology occasionally collapses a one-element collection to the object
    itself; treating that as empty would silently drop a course.
    """
    if not isinstance(payload, dict):
        return []
    value = payload.get(key)
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict):
        return [value]
    return []


# --------------------------------------------------------------------------
# Events  ->  both parse_upcoming_assignments and ical.parse shapes
# --------------------------------------------------------------------------
#
# `/v1/users/<uid>/events?start_date=&end_date=` is one endpoint answering two
# questions the HTML path needed two sources for: the /home "upcoming" widget
# and the iCal feed. Three things it gets right that neither could:
#
#   * **Course attribution is a field, not a guess.** Every row carries `realm`
#     and `section_id`. The documented workaround until now was to fetch
#     `/event/<id>/profile` and dig `realm_id` out of the markup, because event
#     titles are actively misleading -- one course's "Widget Contract Due"
#     reads exactly like another's integrity contract.
#   * **`/event/` items are assignments too.** A teacher can post work as a
#     calendar event, and those never reach the upcoming widget or the
#     gradebook. `type` distinguishes assignment / assessment / event /
#     grade_column; all four are returned, tagged, so nothing is silently
#     dropped for having the wrong URL shape.
#   * The window is server-side, so there is no feed to discover first.
#
# Two traps, both hit live:
#   * The parameters are `start_date` / `end_date` (YYYY-MM-DD). Passing
#     `start` / `end` does not error -- they are read as paging offsets, and the
#     reply is a cheerful `{"event": [], "total": 410}`.
#   * Ids are not unique per occurrence. "Widget Reading Due" came back twice on one day
#     under two ids, because the teacher created it twice. Deduplicating by id
#     leaves both in the list, so the key is (title, start, section_id).

_ASSIGNMENT_TYPES = ("assignment", "assessment", "grade_column")


def course_title(sections_payload, section_id):
    """One section's course title, for stamping `course` on a material row."""
    return _course_lookup(sections_payload or {}).get(_s(section_id))


def _course_lookup(sections_payload):
    """section_id -> course title, for attributing an event to a class."""
    out = {}
    for section in items(sections_payload, "section"):
        sid = _s(section.get("id"))
        if not sid:
            continue
        out[sid] = ": ".join(
            p for p in (_s(section.get("course_title")), _s(section.get("section_title"))) if p
        ) or sid
    return out


def _event_url(event, base_url):
    """The link a human should follow for this row.

    **An event id is not an assignment id.** They are drawn from different
    sequences and do not overlap at all -- across one course's twelve events,
    zero matched an assignment id, and `/assignment/<event_id>` answers 403.
    The event object carries the real one in `assignment_id` (and again inside
    `web_url`), so that is what the link is built from; only rows with no
    assignment behind them fall back to `/event/<id>`.

    Getting this wrong is quiet and expensive: the URL looks perfectly
    plausible, and it is what `get_assignment_info` and any saved note key off.
    """
    if not base_url:
        return None
    assignment_id = _s(event.get("assignment_id"))
    if assignment_id:
        return f"{base_url}/assignment/{assignment_id}"
    eid = _s(event.get("id"))
    return f"{base_url}/event/{eid}" if eid else None


def _dedupe_events(rows):
    """Collapse duplicate occurrences, keeping the first id seen.

    Keyed on (title, start, section) rather than id because a teacher creating
    the same item twice yields two ids for one real deadline -- which is what
    put "Widget Reading Due" into the upcoming list twice.
    """
    seen = {}
    out = []
    for row in rows:
        key = (row.get("title"), row.get("start"), row.get("section_id"))
        if key in seen:
            continue
        seen[key] = True
        out.append(row)
    return out


def events(events_payload, sections_payload=None, base_url=""):
    """`/v1/users/<uid>/events` -> one normalized row per calendar item.

    Carries both vocabularies so a caller can treat the result as upcoming
    assignments (`title`/`due`/`due_iso`) or as calendar events
    (`summary`/`start`/`end`/`all_day`) without a second fetch.
    """
    courses_by_sid = _course_lookup(sections_payload or {})
    rows = []
    for event in items(events_payload, "event"):
        eid = _s(event.get("id"))
        assignment_id = _s(event.get("assignment_id"))
        title = _s(event.get("title"))
        start = _s(event.get("start"))
        end = _s(event.get("end"))
        sid = _s(event.get("section_id"))
        kind = _s(event.get("type")) or "event"
        url = _event_url(event, base_url)
        # Schoology emits "YYYY-MM-DD HH:MM:SS"; ISO wants the T separator, and
        # `due_iso` is what fingerprints and sorting use.
        iso = start.replace(" ", "T")[:16] if start else None

        rows.append({
            # `id` stays the event id -- it is what identifies this occurrence
            # on the calendar. `assignment_id` is the separate, non-overlapping
            # id the gradebook and /assignment/ URLs use.
            "id": eid,
            "assignment_id": assignment_id,
            "title": title,
            "summary": title,          # the iCal vocabulary, same value
            "course": courses_by_sid.get(sid),
            "section_id": sid,
            "realm": _s(event.get("realm")),
            "kind": kind,
            "is_assignment": kind in _ASSIGNMENT_TYPES,
            "due": start,
            "due_iso": iso,
            "start": iso,
            "end": end.replace(" ", "T")[:16] if end else None,
            "all_day": bool(event.get("all_day")),
            "description": _s(event.get("description")),
            "url": url,
            # `due`/`start` are the real timestamps, not a rendered "N days
            # overdue" string, so hashing them is safe here in a way it never
            # was on the HTML path.
            "fp": fp(eid, title, iso, sid, kind),
            "web_url": _s(event.get("web_url")),
        })

    rows = _dedupe_events(rows)
    rows.sort(key=lambda r: (r.get("start") or "", r.get("title") or ""))
    return rows


# --------------------------------------------------------------------------
# Course materials  ->  parsers.parse_course_materials shape
# --------------------------------------------------------------------------
#
# `/v1/courses/<cid>/folder/<fid>` (fid 0 is the root). This is the endpoint
# that retires the server's oldest documented blind spot: the materials page
# could only ever be read one level deep, so an entire unit's worth of work
# sitting in `Unit 1 - Intro/Classwork` was invisible to every tool, and the
# workaround was to hand a folder URL to `download_file` and regex the returned
# HTML. Here nesting is just another request -- `folder-item` carries a `type`
# ("folder" / "assignment" / "document" / "page" / "link"), so the walk needs no
# markup guessing and each level costs ~0.2s.

# The material types the HTML parser emitted, so `type` stays a stable
# vocabulary for callers that switch on it.
_MATERIAL_TYPES = ("folder", "assignment", "assessment", "document", "page",
                   "link", "discussion", "album")

# Schoology ships two quiz engines and reports them as two different types.
# `assessment_v2` is the newer one and was falling through to "unknown", which
# is how two of one unit's three practice quizzes became unreadable while
# the third worked. They are the same thing to a caller -- both are served by
# `/v1/sections/<sid>/assignments/<id>` -- so the newer one is folded into the
# established name and the raw value is preserved in `engine` for anyone who
# needs to tell them apart.
_TYPE_ALIASES = {"assessment_v2": "assessment"}

# Site paths, per type, for the id-only URL we synthesise. Verified live
# 2026-09-02 by fetching each one with the session cookie: `/page/<id>` and
# `/discussion/<id>` answer 200 (the latter redirects to its course-scoped
# form), while `/document/<id>` and `/assessment_v2/<id>` are **404** -- those
# two were the whole "Unrecognized Schoology material URL" bug, because the URL
# this function invented for them exists nowhere on the site.
_SITE_LEAF = {
    "assignment": "assignment",
    "assessment": "assignment",
    "page": "page",
    "discussion": "discussion",
}


def _api_path(location):
    """The `location` self-link reduced to a path this client can GET.

    Schoology returns it absolute and, for some items, against `api.<host>`
    rather than the host we talk to. Only the path is kept, so a row stays
    routable whichever hostname the server felt like naming.
    """
    text = _s(location)
    if not text:
        return None
    path = _re.sub(r"^https?://[^/]+", "", text)
    return path if path.startswith("/v1/") else None


def folder_items(folder_payload, course_id, base_url, folder_title=None):
    """One folder's direct children, in the shape the HTML parser returned.

    `folder` names the containing folder (None at the root), matching the field
    `get_course_materials` already set. `folder_id` is added so a caller can
    keep walking without re-deriving it from a URL.
    """
    out = []
    for item in items(folder_payload, "folder-item"):
        item_id = _s(item.get("id"))
        raw_kind = _s(item.get("type")) or "unknown"
        kind = _TYPE_ALIASES.get(raw_kind, raw_kind)
        title = _s(item.get("title"))
        if not title and not item_id:
            continue
        if kind == "folder":
            url = f"{base_url}/course/{course_id}/materials?f={item_id}"
        elif not item_id:
            url = None
        elif kind in _SITE_LEAF:
            url = f"{base_url}/{_SITE_LEAF[kind]}/{item_id}"
        else:
            # Everything else -- documents above all -- is only reachable at a
            # course-scoped path. `/document/<id>` is a 404, so inventing one
            # from the type name (which is what used to happen here) handed the
            # caller a URL that nothing could open and that `get_material`
            # refused to route.
            url = f"{base_url}/course/{course_id}/materials/{kind}/{item_id}"

        row = {
            "type": kind if kind in _MATERIAL_TYPES else "unknown",
            "title": title,
            "url": url,
            # The server's own /v1 self-link. Worth carrying rather than
            # rebuilding: it names the endpoint *and* the section that serves
            # this item, and a document's section cannot be derived from its
            # id. `get_material` routes on this when it is present.
            "api_path": _api_path(item.get("location")),
            "preview": _s(item.get("description")) or None,
            "folder": folder_title,
            "folder_id": item_id if kind == "folder" else None,
            "available": bool(item.get("available", 1)),
            "due": _s(item.get("due")),
        }
        if raw_kind != kind:
            row["engine"] = raw_kind
        # "file" vs "link": a document row is either an upload or a bookmark,
        # and a caller deciding whether to download something needs to know
        # which before it asks.
        if item.get("document_type"):
            row["document_type"] = _s(item.get("document_type"))
        out.append(row)
    return out


def text_from_html(markup):
    """Readable text out of a stored HTML body.

    A page's body arrives as the markup the teacher composed, where the HTML
    path had already flattened it. Callers switch on `body` being readable
    text, so the flattening has to happen somewhere; bs4 is imported here
    rather than at module scope to keep this module loadable without it, which
    the fingerprint and change-detection paths rely on.
    """
    if not markup:
        return None
    from bs4 import BeautifulSoup
    text = BeautifulSoup(markup, "html.parser").get_text("\n", strip=True)
    return _re.sub(r"\n{3,}", "\n\n", text) or None


def document(payload, base_url, course=None):
    """`/v1/sections/<sid>/documents/<id>` -> the parse_document_info shape.

    One Schoology "document" is either an uploaded file or a bookmarked link,
    and the two arrive in different sub-trees of `attachments`. The HTML page
    for a link showed the destination and the page for a file showed a download
    button, so callers already branch on `type`; that split is preserved here
    rather than inventing a third shape.

    `download_url` comes from the attachment's own `download_path`, which is
    what the site's download button points at -- no `/attachment/...` href has
    to be scraped out of markup that can be restyled at any time.
    """
    attachments = payload.get("attachments") or {}
    files = ((attachments.get("files") or {}).get("file")) or []
    links = ((attachments.get("links") or {}).get("link")) or []
    title = _s(payload.get("title"))

    if links and not files:
        link = links[0]
        return {
            "type": "link",
            "title": title or _s(link.get("title")),
            "course": course,
            "url": _s(link.get("url")),
        }

    out = {
        "type": "document",
        "title": title,
        "course": course,
        "download_url": None,
        "viewer_url": None,
    }
    if files:
        first = files[0]
        out["download_url"] = attachment_url(first.get("download_path"))
        # The "converted" copy is the one the in-browser viewer renders; it is
        # absent until Schoology finishes transcoding, so it stays optional.
        out["viewer_url"] = attachment_url(first.get("converted_download_path")) or None
        out["filename"] = _s(first.get("filename"))
        out["filesize"] = first.get("filesize")
        out["filemime"] = _s(first.get("filemime"))
    if len(files) + len(links) > 1:
        out["attachments"] = [
            {"title": _s(a.get("title")),
             "url": attachment_url(a.get("download_path")) or _s(a.get("url")),
             "filename": _s(a.get("filename")) or None}
            for a in [*files, *links]
        ]
    if not files and not links:
        # A document with nothing attached is a real thing (a teacher can save
        # an empty one), and it is not the same as a failed read.
        out["content_available"] = False
        out["content_note"] = "This document has no file or link attached."
    return out


def page(payload, course=None):
    """`/v1/sections/<sid>/pages/<id>` -> the parse_page_content shape."""
    body_html = _s(payload.get("body")) or None
    return {
        "type": "page",
        "title": _s(payload.get("title")),
        "course": course,
        "body": text_from_html(body_html) if body_html else None,
        "body_html": body_html,
    }


def discussion(payload, base_url, course=None):
    """`/v1/sections/<sid>/discussions/<id>` -> a page-shaped row."""
    body_html = _s(payload.get("body")) or None
    return {
        "type": "discussion",
        "title": _s(payload.get("title")),
        "course": course,
        "body": text_from_html(body_html) if body_html else None,
        "body_html": body_html,
    }


# --------------------------------------------------------------------------
# Messages and activity posts -- change detection only
# --------------------------------------------------------------------------
#
# `get_messages` and `get_recent_posts` stay on the HTML path, because the API
# cannot give them what those tools exist to provide: `/v1/messages/inbox`
# returns `message: null` for every row (no preview, and the only way to get one
# is to open the thread, which marks it read), and `/v1/recent` carries no image
# URLs, which matters because notices are frequently posted as a picture with no
# text at all.
#
# Neither of those gaps affects *change detection*, which only has to answer
# "did a new thread arrive / did a post change". That is cheap and safe over the
# API, so `check_updates` uses these.

# Fields that move on their own and must never reach a fingerprint:
#   messages: `message_status` flips the moment a human opens the thread
#   posts:    `likes` and `num_comments` change without the post changing


def messages(messages_payload, names=None):
    """`/v1/messages/{inbox,sent}` -> the parse_messages row shape.

    `sender` needs a `/v1/users/<id>` lookup, so it is passed in via `names`
    rather than fetched here; without it the row still carries everything
    change detection needs. `preview` is always None -- see above.
    """
    names = names or {}
    out = []
    for message in items(messages_payload, "message"):
        mid = _s(message.get("id"))
        subject = _s(message.get("subject"))
        author = _s(message.get("author_id"))
        updated = message.get("last_updated")
        out.append({
            "id": mid,
            "subject": subject,
            "sender": names.get(author),
            "author_id": author,
            "date_text": _s(updated),
            "preview": None,
            "unread": _s(message.get("message_status")) == "unread",
            "url": None,
            # `unread` is excluded deliberately, exactly as on the HTML path:
            # it flips when a human opens the message, which is not news.
            "fp": fp(mid, subject, author, updated),
        })
    return out


def posts(recent_payload, names=None, base_url=""):
    """`/v1/recent` -> the parse_recent_posts row shape.

    The API body is the *full* text where the feed markup truncated it, but it
    carries no image URLs, so `images` is always empty here. `likes` and
    `num_comments` are returned for context and kept out of the fingerprint --
    a classmate liking a notice is not a change to the notice.
    """
    names = names or {}
    out = []
    for update in items(recent_payload, "update"):
        pid = _s(update.get("id"))
        author = _s(update.get("uid"))
        body = _s(update.get("body"))
        created = update.get("created")
        out.append({
            "id": pid,
            "author": names.get(author),
            "author_id": author,
            "posted_to": _s(update.get("realm")),
            "posted": None,
            "timestamp": created,
            "text": body,
            "images": [],
            "likes": update.get("likes"),
            "num_comments": update.get("num_comments"),
            "url": f"{base_url}/post/{pid}" if pid and base_url else None,
            "fp": fp(pid, author, created, body),
        })
    return out


def school_year_window(today=None):
    """A window that does not move between runs.

    Change detection cannot use the caller's `days_ahead` window: it is
    anchored to *today*, so its contents shift every midnight and the digest
    with them -- one false "calendar changed" alert per day, forever. This is
    the same reason the iCal path hashed the entire feed rather than the
    requested window.

    August through July, so it is stable for a school year and rolls over once.
    That rollover is a real mass change, and the bulk-turnover rule already
    collapses it into a single alert rather than hundreds.
    """
    today = today or _dt.date.today()
    start_year = today.year if today.month >= 8 else today.year - 1
    return _dt.date(start_year, 8, 1), _dt.date(start_year + 1, 7, 31)


_API_ATTACHMENT_RE = _re.compile(
    r"^https://api\.(?P<host>[^/]+)/v1/attachment/(?P<rest>.+)$"
)


def attachment_url(url, base_url=""):
    """Rewrite a /v1 attachment link onto a host whose certificate is valid.

    The API hands back `https://api.<host>/v1/attachment/<id>/source/<hash>`,
    and **that host cannot be fetched by anything**: its certificate covers
    `*.schoology.com`, which does not match a three-label name like
    `api.pausd.schoology.com`. httpx and Playwright both refuse it, so it is not
    a client quirk to work around -- the URL as given is simply dead.

    The same file is served from the main host without the `/v1` segment, which
    is the form the materials page itself links to, and the bytes are identical
    (verified against the same attachment fetched both ways).
    """
    text = _s(url)
    if not text:
        return None
    match = _API_ATTACHMENT_RE.match(text)
    if not match:
        return text
    return f"https://{match.group('host')}/attachment/{match.group('rest')}"


def assignment(detail_payload, sections_payload=None, section_id=None, base_url=""):
    """`/v1/sections/<sid>/assignments/<aid>` -> the parse_assignment_info shape.

    `description` is plain text here where the page gave marked-up HTML, so
    `description_html` is None rather than a copy -- a caller that renders it
    should be able to tell the difference. Attachments come from the payload's
    `attachments` block when Schoology includes one; the page's own attachment
    list is richer, which is part of why the HTML path stays reachable.
    """
    if not isinstance(detail_payload, dict):
        return {"title": None, "course": None, "due": None, "description": None,
                "description_html": None, "attachments": []}

    courses_by_sid = _course_lookup(sections_payload or {})
    attachments = []
    files = detail_payload.get("attachments") or {}
    for group in ("files", "links"):
        block = (files.get(group) or {}).get(group[:-1]) or []
        for att in block if isinstance(block, list) else []:
            url = attachment_url(att.get("download_path")) or _s(att.get("url"))
            if url:
                attachments.append({"name": _s(att.get("title")) or url, "url": url})

    return {
        "title": _s(detail_payload.get("title")),
        "course": courses_by_sid.get(_s(section_id)),
        "due": _s(detail_payload.get("due")),
        "description": _s(detail_payload.get("description")),
        "description_html": None,
        "attachments": attachments,
        "max_points": _s(detail_payload.get("max_points")),
        "web_url": _s(detail_payload.get("web_url")),
    }
