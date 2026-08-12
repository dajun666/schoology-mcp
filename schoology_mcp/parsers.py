"""HTML parsers for Schoology pages.

`parse_courses`, `parse_grades` and their helpers are adapted from the
reference project `dajun666/schoology-get` (export_all_lib.py), whose grade
selectors are confirmed working against PAUSD Schoology.

`parse_upcoming_assignments` and `parse_recent_posts` are new. Schoology's
home/calendar markup is more dynamic, so these are written tolerantly (they
try several selectors and fall back to text extraction). Refine the selectors
against the HTML dumped by `scripts/login_check.py`.
"""

import re
from datetime import datetime

from bs4 import BeautifulSoup

from .fingerprint import fp

# --------------------------------------------------------------------------
# Page skeleton anchors + empty-state markers
# --------------------------------------------------------------------------
#
# An anchor is a container that exists whether or not the page has any items.
# `health.evaluate()` uses them to tell "no grades yet" apart from "the class
# name changed". Verified present in dumps/ -- re-check them after any Schoology
# markup change, because a stale anchor turns every source into a false `error`.

GRADES_ANCHORS = ("ul.s-grades-course-list", "li.s-grades-course-item", "#main-inner")
GRADES_EMPTY_MARKERS = ("no grades", "not enrolled in any courses")

UPCOMING_ANCHORS = (".upcoming-events-wrapper", ".upcoming-submissions-wrapper", "#right-column-inner")
UPCOMING_EMPTY_MARKERS = ("no upcoming", "nothing due")

FEED_ANCHORS = ("#home-feed-container", ".s-edge-feed", "#edge-feed")
FEED_EMPTY_MARKERS = ("no recent activity", "no updates")

COURSES_ANCHORS = ("li.course-item", "div.course-card", "#main-inner")
COURSES_EMPTY_MARKERS = ("not enrolled",)

MATERIALS_ANCHORS = ("#folder-contents-table", ".s-js-materials-body")
MATERIALS_EMPTY_MARKERS = ("no materials", "this folder is empty")

MESSAGES_ANCHORS = ("#main-inner", "#center-inner")
MESSAGES_EMPTY_MARKERS = ("no messages", "your inbox is empty")


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
# Header unread counters (present on every authenticated page)
# --------------------------------------------------------------------------

# Schoology renders `aria-label="0 unread messages"` / `"1 unread notification"`
# in the top bar of every page. This is a server-side change signal that needs
# no caller memory, and it rides along on any page we already fetch. The i18n
# bundle pluralizes, so the pattern must accept both forms.
_UNREAD_RE = re.compile(r"(\d+)\s+unread\s+(message|notification)s?", re.I)


def parse_header_counters(html):
    """Extract the top-bar unread counts. Returns None per key if absent."""
    counters = {"unread_messages": None, "unread_notifications": None}
    for match in _UNREAD_RE.finditer(html or ""):
        count, kind = int(match.group(1)), match.group(2).lower()
        key = "unread_messages" if kind == "message" else "unread_notifications"
        # First occurrence wins; the header renders before any page content.
        if counters[key] is None:
            counters[key] = count
            # Stop once both are known: the header sits in the first few KB and
            # scanning on would walk the ~1.9 MB i18n bundle for nothing.
            if all(v is not None for v in counters.values()):
                break
    return counters


# --------------------------------------------------------------------------
# Submission status (derived from icon CLASSES, never the hidden text)
# --------------------------------------------------------------------------

# Verified in dumps/grades.html. The visually-hidden sentence next to each icon
# is human-readable but is also folded into the grade cell's text by
# `_parse_grade_cell`, so deriving from the class list keeps the two independent.
#
# IMPORTANT: the absence of an icon means "this row has no dropbox", NOT "the
# student did not submit". Many rows never have one. There is deliberately no
# `not_submitted` value -- alerting on absence would be wrong.
_SUBMISSION_BY_CLASS = (
    ("grade-pending-icon", "submitted_ungraded"),
    ("dropbox-icon-inline-image-wrapper", "submitted"),
    ("common-assessment-icon", "completed_assessment"),
    ("has-discussion-comment", "posted_discussion"),
)


def _submission_status(row):
    """Infer submission state from the row's icon classes. Never 'not_submitted'."""
    classes = set()
    for span in row.select("span[class]"):
        classes.update(span.get("class") or [])
    for marker, status in _SUBMISSION_BY_CLASS:
        if marker in classes:
            return status
    return "unknown"


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
    """Parse the gradebook.

    Each row carries two fingerprints so a caller can tell an alert-worthy
    change from noise:
      `fp`      -- grade, comment, submission state: "a score was posted"
      `fp_meta` -- title, due date, category weight: "the teacher fixed a typo"
    Neither includes anything that changes on its own.
    """
    soup = BeautifulSoup(html, "html.parser")
    courses = []
    for course_item in soup.select("li.s-grades-course-item"):
        course_title = _clean_text(course_item.select_one(".gradebook-course-title a"))
        course_grade = _clean_text(
            course_item.select_one(".summary-course .course-grade-value")
        )
        # The <li> itself has no id; the course id lives on its own course-row
        # (`data-parent-id=""` marks it as the root of the course's tree).
        course_row = course_item.select_one("tr.report-row.course-row")
        course_id = course_row.get("data-id") if course_row else None
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
            row_id = row.get("data-id")
            parent_id = row.get("data-parent-id") or None
            grade = _parse_grade_cell(row.select_one("td.grade-column"))
            comment = _clean_text(row.select_one("td.comment-column .comment"))
            submission = _submission_status(row)
            percentage_contrib = _clean_text(row.select_one(".percentage-contrib"))
            due = _clean_text(row.select_one(".due-date"))

            # Period-row ids repeat across courses ("1120687", and "0" for the
            # ungraded bucket), so the row id alone is not unique gradebook-wide.
            # Qualify it with the course to get a key safe for a flat snapshot.
            uid = f"{course_id or course_title}:{row_id}"

            rows.append(
                {
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
            )
        courses.append(
            {
                "id": course_id,
                "title": course_title,
                "course_grade": course_grade,
                "rows": rows,
                "fp": fp(course_id or course_title, course_grade),
            }
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

        # Identity: the numeric id in the url (/assignment/N, /event/N).
        item_id = None
        if url:
            m = re.search(r"/(?:assignment|event)/(\d+)", url)
            item_id = m.group(1) if m else url
        item_id = item_id or title

        item = {
            "id": item_id,
            "title": title,
            "course": course,
            "due": due,
            "due_iso": due_iso,
            "url": url,
            # `due` is deliberately NOT hashed: it renders as "80 days overdue",
            # which changes every midnight and would fire a false "assignment
            # changed" alert daily, forever. `due_iso` is the real due time.
            "fp": fp(item_id, title, course, due_iso),
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

# Post bodies are rich text, and school notices are routinely posted AS an
# image with no words at all -- `.update-body` then yields the empty string and
# the post looks blank to a reader. Three kinds of <img> appear and only one is
# content:
#   - profile avatars (`imagecache-profile_*`): the same few URLs on every post
#   - emoji, served as images by Google Fonts with the character in `alt`
#   - the actual embedded picture (`/system/files/attachments/page_embeds/...`
#     or `/file_download/...`), which needs the session cookie to fetch
_AVATAR_CLASSES = ("imagecache-profile_sm", "imagecache-profile_reg", "profile-picture")
_EMOJI_HOSTS = ("fonts.gstatic.com", "notoemoji")


def _is_avatar(img) -> bool:
    classes = " ".join(img.get("class") or [])
    if any(c in classes for c in _AVATAR_CLASSES):
        return True
    return any(c in (img.get("src") or "") for c in ("profile-image", "profile_sm"))


def _is_emoji(img) -> bool:
    return any(h in (img.get("src") or "") for h in _EMOJI_HOSTS)


def _extract_post_images(body, base_url):
    """Pull content images out of a post body, folding emoji back into text.

    Emoji are replaced in place by their `alt` character so the post's text
    reads the way a human sees it; avatars are dropped; everything else is
    returned for the caller to fetch.
    """
    images = []
    if body is None:
        return images
    for img in body.select("img"):
        if _is_emoji(img):
            img.replace_with(img.get("alt") or "")
            continue
        if _is_avatar(img):
            continue
        src = img.get("src")
        if not src:
            continue
        images.append({
            "url": absolute_url(src, base_url),
            "alt": (img.get("alt") or "").strip() or None,
        })
    return images


def parse_recent_posts(html, base_url, limit=20):
    """Extract recent activity-feed posts from a Schoology home page."""
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for li in soup.select(_FEED_ITEM_SELECTOR):
        # Most posts carry text in `.update-body`; link/file shares put their
        # content in `.edge-main` instead.
        body = li.select_one(".update-body") or li.select_one(".edge-main")
        # Must run before the text is extracted: it substitutes emoji images
        # for their characters, which would otherwise vanish entirely.
        images = _extract_post_images(body, base_url)
        text = _clean_text(body)
        author = _clean_text(li.select_one(".long-username a")) or _clean_text(
            li.select_one(".edge-left a[title]")
        )
        # An image-only post has no text at all; dropping it here would hide
        # notices that are posted purely as a picture.
        if not text and not author and not images:
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
        timestamp = int(ts) if ts and ts.isdigit() else None
        # `li id="edge-assoc-NNNNNNNNNNN"` is the post's own stable id. Note
        # `url` below is the *realm* link (/course/N), shared by every post in a
        # course -- it is not an identity key.
        post_id = (li.get("id") or "").replace("edge-assoc-", "") or None

        posts.append(
            {
                "id": post_id,
                "author": author,
                "posted_to": posted_to,
                # `posted` is empty in the server HTML (JS fills it in), so it is
                # render-dependent and excluded from the fingerprint.
                "posted": _clean_text(li.select_one(".edge-footer .created")),
                "timestamp": timestamp,
                "text": text,
                "images": images,
                "url": posted_to_url,
                # Image COUNT, not the URLs: an added or removed picture is a
                # real change, but the embed URLs carry generated suffixes whose
                # stability across renders is unverified, and a URL that churns
                # would fire a false alert on every run.
                "fp": fp(post_id, author, posted_to, timestamp, text, len(images)),
            }
        )
        if len(posts) >= limit:
            break
    return posts


# --------------------------------------------------------------------------
# Private messages (verified against real /messages and /messages/sent dumps)
# --------------------------------------------------------------------------

# Schoology's inbox is the Drupal privatemsg module:
#   table.privatemsg-list > tr.odd|tr.even
#   td.privatemsg-list-subject[subject="<FULL subject>"]   <- untruncated
#     a.subject-link[href="/messages/view/<thread id>"]    <- truncated text
#     p.privatemsg-list-body                               <- body preview
#     p.names-date > a[href^="/user/"] + span.small.gray   <- sender + date
#
# UNVERIFIED: the unread indicator. Every message in the account was already
# read when this was written (the header reported 0 unread), so no unread row
# existed to inspect. Rather than guess a class and report every message as
# read, `unread` is left None when nothing recognizable is found -- callers
# should trust `unread_count` from the page header instead.
_MESSAGE_ROW_SELECTOR = "table.privatemsg-list tr"
MESSAGE_ID_RE = re.compile(r"/messages/view/(\d+)")
_UNREAD_ROW_MARKERS = ("unread", "privatemsg-unread", "new")


def _row_unread(row):
    """True if the row is recognizably unread, else None (unknown)."""
    classes = {c.lower() for c in (row.get("class") or [])}
    if classes & set(_UNREAD_ROW_MARKERS):
        return True
    if row.select_one(".unread, .privatemsg-unread, strong.subject-link"):
        return True
    return None


def parse_messages(html, base_url):
    """Parse an inbox / sent-messages listing.

    Returns `{"id","subject","sender","date_text","preview","unread","url","fp"}`.
    `unread` is excluded from `fp` -- it flips whenever a human opens the
    message in the real Schoology UI, which is not a change worth alerting on.
    """
    soup = BeautifulSoup(html, "html.parser")
    messages = []
    for row in soup.select(_MESSAGE_ROW_SELECTOR):
        cell = row.select_one("td.privatemsg-list-subject")
        if not cell:
            continue  # header row
        link = cell.select_one("a.subject-link") or cell.find(
            "a", href=MESSAGE_ID_RE
        )
        href = link.get("href") if link else None
        match = MESSAGE_ID_RE.search(href or "")
        thread_id = match.group(1) if match else None

        # The cell's `subject` attribute holds the full subject; the link text
        # is ellipsized for display.
        subject = cell.get("subject") or _clean_text(link)

        names_date = cell.select_one("p.names-date")
        sender = date_text = None
        if names_date:
            sender = _clean_text(names_date.find("a", href=re.compile(r"^/user/")))
            date_text = _clean_text(names_date.select_one("span.small.gray"))
        if not sender:
            picture_link = cell.select_one(".picture a[title]")
            sender = picture_link.get("title") if picture_link else None

        preview = _clean_text(cell.select_one("p.privatemsg-list-body"))

        if not thread_id and not subject:
            continue

        messages.append(
            {
                "id": thread_id,
                "subject": subject,
                "sender": sender,
                "date_text": date_text,
                "preview": preview,
                "unread": _row_unread(row),
                "url": absolute_url(href, base_url) if href else None,
                "fp": fp(thread_id, subject, sender, date_text, preview),
            }
        )
    return messages


def parse_message_thread(html, base_url):
    """Parse one message thread (/messages/view/NNN).

    Each `.message-body` block holds one message: sender, timestamp and the
    full text. Attachments, when present, sit in a nested attachments block.
    """
    soup = BeautifulSoup(html, "html.parser")
    messages = []
    for block in soup.select(".message-body"):
        name_el = block.select_one(".name")
        author = date_text = None
        if name_el:
            author = _clean_text(name_el.find("a"))
            date_text = _clean_text(name_el.select_one("span.small.gray"))

        # The body is everything except the name header and attachments block.
        body_parts = []
        for para in block.find_all("p", recursive=False):
            text = _clean_text(para)
            if text:
                body_parts.append(text)
        if not body_parts:
            clone = _clean_text(block) or ""
            for strip in (author, date_text):
                if strip:
                    clone = clone.replace(strip, "", 1)
            body_parts = [clone.strip()] if clone.strip() else []

        attachments = []
        seen = set()
        for a in block.select(".attachments a[href], .s-message-attachments a[href]"):
            url = absolute_url(a.get("href"), base_url)
            if url and url not in seen:
                seen.add(url)
                attachments.append({"name": _clean_text(a) or url, "url": url})

        messages.append(
            {
                "author": author,
                "date_text": date_text,
                "text": "\n\n".join(body_parts) or None,
                "attachments": attachments,
            }
        )

    subject = _strip_schoology_suffix(_clean_text(soup.title)) if soup.title else None
    # The <h1> on a thread page is the generic "Messages"; prefer the real
    # subject rendered in the thread header.
    header = _clean_text(soup.select_one(".message-thread-subject, #main-inner h2"))
    return {
        "subject": header or subject,
        "messages": messages,
        "count": len(messages),
    }


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
