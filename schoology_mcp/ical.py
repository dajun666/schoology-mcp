"""Schoology calendar via its private iCal feed.

Schoology exposes the student's whole calendar as an .ics feed. Compared with
scraping the calendar page this is a much better source: one HTTP request with
no browser rendering, and 709 structured events at PAUSD instead of a month
grid that renders its events through JavaScript.

Discovery: any authenticated page embeds the numeric user id as `"uid":"…"`;
`/calendar/feed/export/user/<uid>` then serves a dialog containing a
`webcal://…/ical.ics` URL, which is the same resource over https.

Verified against the live PAUSD feed (709 VEVENTs):
  - properties present: DTSTAMP, DTSTART, DTEND, UID, URL, SUMMARY, DESCRIPTION
  - **no LAST-MODIFIED and no SEQUENCE**, so the feed offers no "changed at"
    signal of its own -- content fingerprints are the only way to detect edits
  - DTSTART/DTEND come as `VALUE=DATE` (all-day) or `VALUE=DATE-TIME` in UTC
    with a `Z` suffix; no TZID anywhere
  - every event linked to `/event/<id>`; **no assignments appeared in the feed**,
    so assignment due dates must still come from the /home widget

DTSTAMP is regenerated on every request, so hashing the raw .ics bytes would
report "the calendar changed!" every single run. It is excluded from `fp`.
"""

import re
from datetime import datetime, timedelta, timezone

from .fingerprint import fp

_EVENT_ID_RE = re.compile(r"calendar-event-(\d+)")
_USER_ID_RE = re.compile(r'"uid"\s*:\s*"?(\d+)"?')
_ICAL_HREF_RE = re.compile(r"(webcal|https?)://[^\s\"'<>]+?\.ics", re.I)


# --------------------------------------------------------------------------
# Feed discovery
# --------------------------------------------------------------------------

def user_id_from_html(html):
    """Pull the numeric Schoology user id out of any authenticated page."""
    match = _USER_ID_RE.search(html or "")
    return match.group(1) if match else None


def export_path(user_id):
    return f"/calendar/feed/export/user/{user_id}"


def feed_url_from_export_html(html, base_url=""):
    """Extract the .ics URL from the calendar export dialog.

    The dialog offers a `webcal://` URL for calendar apps; the same path over
    https is what we can fetch with the session's cookies.
    """
    match = _ICAL_HREF_RE.search(html or "")
    if not match:
        return None
    url = match.group(0)
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    return url


# --------------------------------------------------------------------------
# .ics parsing
# --------------------------------------------------------------------------

def _unfold(text):
    """Join RFC 5545 continuation lines (a line beginning with space/tab).

    The live feed happens not to fold today, but folding is legal and would
    silently truncate long SUMMARY/DESCRIPTION values if ignored.
    """
    out = []
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _unescape(value):
    """Decode RFC 5545 text escapes (\\, \\; \\n \\\\)."""
    out = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append({"n": "\n", "N": "\n"}.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _split_line(line):
    """Split `NAME;PARAM=X:value` into (name, params_str, value)."""
    idx = line.find(":")
    if idx == -1:
        return None, "", ""
    head, value = line[:idx], line[idx + 1:]
    if ";" in head:
        name, params = head.split(";", 1)
    else:
        name, params = head, ""
    return name.upper().strip(), params.upper(), value


def _to_iso(raw, params):
    """Normalize a DTSTART/DTEND value to ISO-8601. Returns (iso, all_day)."""
    raw = (raw or "").strip()
    if not raw:
        return None, False
    if "VALUE=DATE-TIME" in params or "T" in raw:
        m = re.match(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)", raw)
        if m:
            y, mo, d, h, mi, s, z = m.groups()
            return f"{y}-{mo}-{d}T{h}:{mi}:{s}{'Z' if z else ''}", False
        return raw, False
    m = re.match(r"(\d{4})(\d{2})(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}", True
    return raw, False


def parse_ics(text):
    """Parse a VCALENDAR body into a list of event dicts.

    Each event: id, uid, summary, description, url, start, end, all_day, fp.
    `fp` hashes the RAW property values, so it is independent of this machine's
    timezone and of DTSTAMP.
    """
    events = []
    current = None
    for line in _unfold(text):
        stripped = line.strip()
        if stripped == "BEGIN:VEVENT":
            current = {}
            continue
        if stripped == "END:VEVENT":
            if current is not None:
                events.append(_build_event(current))
            current = None
            continue
        if current is None:
            continue
        name, params, value = _split_line(line)
        if name:
            current[name] = (params, value)
    return events


def _build_event(props):
    def raw(name):
        return props.get(name, ("", ""))[1]

    def params(name):
        return props.get(name, ("", ""))[0]

    uid = raw("UID").strip()
    match = _EVENT_ID_RE.search(uid)
    start_iso, all_day = _to_iso(raw("DTSTART"), params("DTSTART"))
    end_iso, _ = _to_iso(raw("DTEND"), params("DTEND"))

    return {
        "id": match.group(1) if match else (uid or None),
        "uid": uid or None,
        "summary": _unescape(raw("SUMMARY")).strip() or None,
        "description": _unescape(raw("DESCRIPTION")).strip() or None,
        "url": _unescape(raw("URL")).strip() or None,
        "start": start_iso,
        "end": end_iso,
        "all_day": all_day,
        # DTSTAMP deliberately excluded -- it is regenerated per request.
        "fp": fp(uid, raw("DTSTART"), raw("DTEND"), raw("SUMMARY"),
                 raw("DESCRIPTION"), raw("URL")),
    }


# --------------------------------------------------------------------------
# Windowing
# --------------------------------------------------------------------------

def window(events, days_back=0, days_ahead=14, now=None):
    """Events whose start date falls in [today-days_back, today+days_ahead].

    ISO date strings sort lexicographically, so this is a plain string compare.

    Caveat: timed events are stored in UTC, so one near local midnight can land
    on the neighbouring day. For a multi-day window that is a boundary nit, not
    worth dragging timezone conversion into the comparison.
    """
    today = (now or datetime.now()).date()
    lo = (today - timedelta(days=days_back)).isoformat()
    hi = (today + timedelta(days=days_ahead)).isoformat()
    out = []
    for ev in events:
        start = ev.get("start")
        if not start:
            continue
        if lo <= start[:10] <= hi:
            out.append(ev)
    out.sort(key=lambda e: (e.get("start") or "", e.get("summary") or ""))
    return out


def search(events, query):
    """Case-insensitive substring match over summary + description."""
    if not query:
        return events
    needle = query.lower()
    return [
        e for e in events
        if needle in (e.get("summary") or "").lower()
        or needle in (e.get("description") or "").lower()
    ]
