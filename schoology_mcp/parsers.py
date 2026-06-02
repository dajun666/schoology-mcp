"""HTML parsers for Schoology pages.

`parse_courses`, `parse_grades` and their helpers are adapted from the
reference project `dajun666/schoology-get` (export_all_lib.py), whose grade
selectors are confirmed working against PAUSD Schoology.

`parse_upcoming_assignments` and `parse_recent_posts` are new. Schoology's
home/calendar markup is more dynamic, so these are written tolerantly (they
try several selectors and fall back to text extraction). Refine the selectors
against the HTML dumped by `scripts/login_check.py`.
"""

from datetime import datetime

from bs4 import BeautifulSoup


# --------------------------------------------------------------------------
# Shared helpers (from the reference project)
# --------------------------------------------------------------------------

def absolute_url(url, base_url):
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if not url.startswith("/"):
        return f"{base_url}/{url}"
    return f"{base_url}{url}"


def _clean_text(el):
    if not el:
        return None
    text = " ".join(el.stripped_strings)
    return text if text else None


# --------------------------------------------------------------------------
# Courses (from the reference project)
# --------------------------------------------------------------------------

def parse_courses(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    courses = []
    for card in soup.select("li.course-item, div.course-card"):
        title = card.get_text(" ", strip=True)
        link = None
        a = card.find("a")
        if a and a.get("href"):
            link = absolute_url(a["href"], base_url)
        if title:
            courses.append({"title": title, "url": link})
    return courses


# --------------------------------------------------------------------------
# Grades (from the reference project)
# --------------------------------------------------------------------------

def _parse_grade_cell(td):
    if not td:
        return None
    raw_text = _clean_text(td)
    alpha_el = td.select_one(".alpha-grade")
    numeric_el = td.select_one(".rounded-grade")
    max_el = td.select_one(".max-grade")
    data = {"raw": raw_text}
    if alpha_el:
        data["alpha"] = _clean_text(alpha_el)
    if numeric_el:
        data["numeric"] = numeric_el.get("title") or _clean_text(numeric_el)
    if max_el:
        max_text = _clean_text(max_el)
        if max_text:
            data["max"] = max_text.replace("/", "").strip()
    return data


def parse_grades(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    courses = []
    for course_item in soup.select("li.s-grades-course-item"):
        course_title = _clean_text(course_item.select_one(".gradebook-course-title a"))
        course_grade = _clean_text(
            course_item.select_one(".summary-course .course-grade-value")
        )
        rows = []
        for row in course_item.select("tr.report-row"):
            classes = row.get("class") or []
            row_type = None
            for t in ("course-row", "period-row", "category-row", "item-row"):
                if t in classes:
                    row_type = t.replace("-row", "")
                    break
            title_el = row.select_one("th.title-column .title")
            title = _clean_text(title_el)
            assignment_url = None
            if title_el:
                link = title_el.find("a")
                if link and link.get("href"):
                    assignment_url = absolute_url(link["href"], base_url)
            rows.append(
                {
                    "id": row.get("data-id"),
                    "parent_id": row.get("data-parent-id") or None,
                    "type": row_type,
                    "title": title,
                    "percentage_contrib": _clean_text(
                        row.select_one(".percentage-contrib")
                    ),
                    "due": _clean_text(row.select_one(".due-date")),
                    "grade": _parse_grade_cell(row.select_one("td.grade-column")),
                    "comment": _clean_text(
                        row.select_one("td.comment-column .comment")
                    ),
                    "assignment_url": assignment_url,
                }
            )
        courses.append(
            {"title": course_title, "course_grade": course_grade, "rows": rows}
        )
    return courses


# --------------------------------------------------------------------------
# Upcoming assignments (new — tolerant; verify selectors via login_check dump)
# --------------------------------------------------------------------------

# Each upcoming item is a `.upcoming-event` block on the Schoology home page.
# Inside it: `.event-title a` is the assignment, `.event-subtitle` spans carry
# the due / "N days overdue" text and course short-name, `.realm-main-titles`
# the full course name, and a `data-start` attribute the due time as a unix
# timestamp.
_UPCOMING_SELECTOR = ".upcoming-event"


def parse_upcoming_assignments(html, base_url):
    """Extract upcoming / due-soon items from a Schoology home page."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    by_key = {}
    for el in soup.select(_UPCOMING_SELECTOR):
        title_el = el.select_one(".event-title a") or el.find("a", href=True)
        title = _clean_text(title_el)
        if not title:
            continue
        url = None
        if title_el and title_el.get("href"):
            url = absolute_url(title_el["href"], base_url)

        subtitles = [
            t for t in (_clean_text(s) for s in el.select(".event-subtitle")) if t
        ]
        # First subtitle is the due / "N days overdue" text. Calendar-style
        # events have no subtitle but carry the time in `.upcoming-time`.
        due = subtitles[0] if subtitles else _clean_text(el.select_one(".upcoming-time"))
        # Prefer the full course name; fall back to the short-code subtitle.
        course = _clean_text(el.select_one(".realm-main-titles"))
        if not course and len(subtitles) > 1:
            course = subtitles[1]

        # `data-start` is the due time as a unix timestamp -- machine-readable.
        due_iso = None
        ds = el.get("data-start")
        if ds and ds.lstrip("-").isdigit():
            try:
                due_iso = datetime.fromtimestamp(int(ds)).isoformat(timespec="minutes")
            except (ValueError, OSError, OverflowError):
                due_iso = None

        item = {
            "title": title,
            "course": course,
            "due": due,
            "due_iso": due_iso,
            "url": url,
        }

        # Schoology can render the same assignment twice on /home: once as a
        # hidden "N days overdue" warning and once as the normal upcoming item.
        # Deduplicate by URL and prefer the more informative human due string.
        key = url or title
        previous = by_key.get(key)
        if previous:
            old_due = (previous.get("due") or "").lower()
            new_due = (due or "").lower()
            if "overdue" in old_due and new_due.startswith("due "):
                by_key[key] = item
            elif not previous.get("due") and due:
                by_key[key] = item
            elif not previous.get("due_iso") and due_iso:
                previous["due_iso"] = due_iso
            continue
        by_key[key] = item
        items.append(item)

    return [by_key[item.get("url") or item.get("title")] for item in items if (item.get("url") or item.get("title")) in by_key]


# --------------------------------------------------------------------------
# Single assignment page (verified against a real /assignment/NNN dump)
# --------------------------------------------------------------------------

# The Schoology assignment-detail page renders:
#   - <title> "<assignment name> | Schoology"
#   - <nav><span class="course-title"><a href="/course/NNN">Course full name</a>
#   - a `.due-date` span -- second match has the human "Due: ..." text
#   - the description in `.info-body` (also tagged `.s-rte`); attachments, if
#     any, sit in a sibling `.attachments`/`.attachments-file-name a` block.
def _strip_schoology_suffix(text):
    if not text:
        return text
    return text.split(" | Schoology")[0].strip() or None


def _first_course_breadcrumb(soup):
    """Find the visible course-title link in the page's top nav."""
    span = soup.select_one("nav span.course-title a, span.course-title a")
    if span:
        text = _clean_text(span)
        if text and "Profile" not in text:
            return text
    # Fallback: any /course/NNN link whose text isn't a nav item.
    import re as _re
    for a in soup.find_all("a", href=_re.compile(r"^/course/\d+$")):
        text = _clean_text(a)
        if text and "Profile" not in text and "Materials" not in text:
            return text
    return None


def parse_assignment_info(html, base_url):
    """Extract the title/course/due/description/attachments of one assignment."""
    soup = BeautifulSoup(html, "html.parser")

    # Title: <title> is the cleanest source; #main h1 is a backup.
    title = _strip_schoology_suffix(_clean_text(soup.title)) if soup.title else None
    if not title:
        title = _clean_text(soup.select_one("#main h1, #main-inner h1"))

    course = _first_course_breadcrumb(soup)

    # Due: prefer the .due-date that actually contains the "Due:" text;
    # the first match is sometimes a sibling stub left empty by Schoology.
    due = None
    for el in soup.select(".due-date"):
        text = _clean_text(el)
        if text and "Due" in text:
            due = text.split(":", 1)[1].strip() if ":" in text else text
            break

    # Description body: tolerant cascade. First non-empty container wins.
    body_el = None
    for sel in (".info-body", ".s-rte", "#assignment-body", ".body"):
        candidate = soup.select_one(sel)
        if candidate and _clean_text(candidate):
            body_el = candidate
            break

    description = _clean_text(body_el)
    description_html = body_el.decode_contents().strip() if body_el else None

    # Attachments: any link inside an attachments-style container. Dedup by URL.
    attachments = []
    seen_urls = set()
    for sel in (
        ".attachments-file-name a[href]",
        ".attachments a[href]",
        ".attachment a[href]",
        ".s-attachment a[href]",
    ):
        for a in soup.select(sel):
            href = a.get("href")
            if not href:
                continue
            full = absolute_url(href, base_url)
            if full in seen_urls:
                continue
            seen_urls.add(full)
            attachments.append({"name": _clean_text(a) or full, "url": full})

    return {
        "title": title,
        "course": course,
        "due": due,
        "description": description,
        "description_html": description_html,
        "attachments": attachments,
    }


# --------------------------------------------------------------------------
# Recent posts / activity feed (verified against a real /home dump)
# --------------------------------------------------------------------------

# Each feed post is `<li id="edge-assoc-NNN" timestamp="...">` in the home
# feed. Inside: `.long-username a` is the author, the `/course|/group` link in
# `.update-sentence-inner` is where it was posted, `.update-body` the text, and
# `.edge-footer .created` the human-readable time.
_FEED_ITEM_SELECTOR = "li[id^='edge-assoc-']"
_REALM_HREF_HINTS = ("/course/", "/group/", "/school/")


def parse_recent_posts(html, base_url, limit=20):
    """Extract recent activity-feed posts from a Schoology home page."""
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for li in soup.select(_FEED_ITEM_SELECTOR):
        # Most posts carry text in `.update-body`; link/file shares put their
        # content in `.edge-main` instead.
        text = _clean_text(li.select_one(".update-body")) or _clean_text(
            li.select_one(".edge-main")
        )
        author = _clean_text(li.select_one(".long-username a")) or _clean_text(
            li.select_one(".edge-left a[title]")
        )
        if not text and not author:
            continue

        # The realm a post went to: first course/group/school link in the
        # update sentence.
        posted_to = posted_to_url = None
        sentence = li.select_one(".update-sentence-inner")
        if sentence:
            for a in sentence.find_all("a", href=True):
                if any(h in a["href"] for h in _REALM_HREF_HINTS):
                    posted_to = _clean_text(a)
                    posted_to_url = absolute_url(a["href"], base_url)
                    break

        ts = li.get("timestamp")
        posts.append(
            {
                "author": author,
                "posted_to": posted_to,
                "posted": _clean_text(li.select_one(".edge-footer .created")),
                "timestamp": int(ts) if ts and ts.isdigit() else None,
                "text": text,
                "url": posted_to_url,
            }
        )
        if len(posts) >= limit:
            break
    return posts


# --------------------------------------------------------------------------
# Course materials (verified against real /course/NNN/materials dumps)
# --------------------------------------------------------------------------

# Materials page uses #folder-contents-table with rows:
#   tr.material-row-folder            → folder, link to ?f=NNN
#   tr.dr.type-assignment             → assignment, link to /assignment/NNN
#   tr.dr.type-document               → file/link/PDF, link to /course/.../materials/...
#   tr.dr.type-page                   → page, link to /page/NNN
#   tr.dr.type-discussion             → discussion (same pattern)
#
# All items have a single td.folder-contents-cell with the title in the first <a>.

def _material_type_from_row(row):
    """Infer material type from the TR's class list."""
    classes = row.get("class") or []
    if "material-row-folder" in classes:
        return "folder"
    for cls in classes:
        if cls.startswith("type-"):
            return cls[5:]  # e.g. "assignment", "document", "page", "discussion"
    return "unknown"


def parse_course_materials(html, base_url):
    """Extract materials from a Schoology course materials page (root or folder).

    Returns a list of dicts with keys: type, title, url, preview.
    For folders the url points to the folder page (?f=NNN).
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#folder-contents-table")
    if not table:
        return []
    items = []
    for row in table.select("tr"):
        item_type = _material_type_from_row(row)
        td = row.select_one("td.folder-contents-cell")
        if not td:
            continue
        a = td.find("a", href=True)
        title = _clean_text(a) if a else _clean_text(td)
        if not title:
            continue
        url = absolute_url(a["href"], base_url) if a else None
        # Strip the type-label prefix (e.g. "Expand folder. Folder. Title..." → "Title")
        full_text = _clean_text(td) or ""
        preview = None
        if item_type == "folder" and title and title in full_text:
            after = full_text[full_text.index(title) + len(title):].strip()
            if after:
                preview = after[:200]
        items.append({"type": item_type, "title": title, "url": url, "preview": preview})
    return items


# --------------------------------------------------------------------------
# Individual material pages (page, document/file, external link)
# --------------------------------------------------------------------------

def _strip_lesson_plan_suffix(text):
    if not text:
        return text
    import re as _re
    return _re.sub(r"\s*\d+\s+lesson plans?\s*$", "", text).strip() or None


def parse_page_content(html, base_url):
    """Extract content from a Schoology Page (/page/NNN)."""
    soup = BeautifulSoup(html, "html.parser")
    title = _strip_lesson_plan_suffix(_clean_text(soup.select_one("h1.page-title")))
    course = _first_course_breadcrumb(soup)
    body_el = soup.select_one(".s-rte, .info-body, .page-body")
    body = _clean_text(body_el)
    body_html = body_el.decode_contents().strip() if body_el else None
    return {"type": "page", "title": title, "course": course, "body": body, "body_html": body_html}


def parse_document_info(html, base_url):
    """Extract info from a Schoology file/document page (/materials/gp/NNN)."""
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one("h1.page-title")
    title = _clean_text(title_el)
    if title:
        title = title.split(" 0 lesson plans")[0].strip()
    course = _first_course_breadcrumb(soup)
    download_url = viewer_url = None
    for a in soup.select("a[href*='/attachment/']"):
        href = a["href"]
        if "/source/" in href and not download_url:
            download_url = absolute_url(href, base_url)
        elif "/docviewer" in href and not viewer_url:
            viewer_url = absolute_url(href, base_url)
    return {
        "type": "document",
        "title": title,
        "course": course,
        "download_url": download_url,
        "viewer_url": viewer_url,
    }


def parse_link_info(html, base_url):
    """Extract the external URL from a Schoology link material page (/materials/link/view/NNN)."""
    import urllib.parse as _up
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one("h1.page-title")
    title = _clean_text(title_el)
    if title:
        title = title.split(" 0 lesson plans")[0].strip()
    course = _first_course_breadcrumb(soup)
    external_url = None
    for a in soup.select("a[href*='/link?']"):
        href = a["href"]
        qs = _up.parse_qs(_up.urlparse(href).query)
        if "path" in qs:
            external_url = qs["path"][0]
            break
    return {"type": "link", "title": title, "course": course, "url": external_url}
