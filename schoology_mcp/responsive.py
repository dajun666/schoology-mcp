"""Responsive Scheduling -- the flex period the student picks a teacher for.

Infinite Campus names the feature "Responsive Scheduling"; a school names the
period whatever it likes (Flex, Tutorial, Advisory). One **session** is one flex
block on one day; one **offering** is one teacher's room inside it, with a seat
cap. The student holds at most one offering per session and may swap it for as
long as the session is open.

Two portal endpoints, both under the undocumented `prism` namespace:

    GET  /campus/resources/prism/portal/responsiveSchedule
         ?calendarID=&personID=&structureID=
         -> [{responsiveSessionID, sessionName, startDate, endDate,
              sessionOpen, offerings: [...]}]
    POST /campus/resources/prism/portal/responsiveSchedule/update
         {personID, responsiveSessionID, calendarID, responsiveOfferingID}
         -> {"success": bool, "message": str}

All three query ids sit on **every roster entry**, which is why `enrollment()`
reads them from the roster the schedule tool already fetches. The portal's own
SPA takes them from `/campus/api/portal/students` instead -- the endpoint that
also returns the student's legal name, district number and state ID, and that
this server does not read.

This module is a pure transform, like `campus.py`: no I/O, so the fixture suite
can exercise all of it.

**The eligibility rules below are the portal's, mirrored field for field**, not
a house policy -- read from its bundle so that a refusal here means the same
thing the web UI's greyed-out row means, rather than an optimistic POST that
comes back `success: false`:

- an offering is full when `maxStudents - currentStudents <= 0`, except for the
  one currently held (leaving and re-entering your own seat is not a new seat)
- the offering carrying a non-zero `rosterID` is the one currently held
- `teacherRequest` on *that* offering locks the whole session: a teacher or
  administrator placed the student, and the student may not overrule it
- a session with exactly one offering that is already held is not a choice
- a save with no actual change is rejected by the portal, so it is refused here
"""

from datetime import datetime

SCHEDULE_PATH = "/campus/resources/prism/portal/responsiveSchedule"
UPDATE_PATH = "/campus/resources/prism/portal/responsiveSchedule/update"


def enrollment(roster):
    """The `{personID, calendarID, structureID}` triple the endpoints want.

    Every roster entry carries all three and they are identical across a single
    student's enrolment, so the first usable entry answers it. Returns `None`
    when the roster is empty or shaped unexpectedly -- the caller reports that
    as "no enrolment", never as a crash.
    """
    for entry in roster if isinstance(roster, list) else []:
        if not isinstance(entry, dict):
            continue
        ids = {k: entry.get(k) for k in ("personID", "calendarID", "structureID")}
        if all(v is not None for v in ids.values()):
            return ids
    return None


def query(ids):
    """Query string for `SCHEDULE_PATH`, given an `enrollment()` triple."""
    return (f"?calendarID={ids['calendarID']}&personID={ids['personID']}"
            f"&structureID={ids['structureID']}")


def _date(epoch_ms):
    """`startDate` is midnight **in the school's timezone**, as epoch millis.

    Converted in local time deliberately: reading it as UTC moves the session
    onto the previous or next calendar day for anyone whose clock is not the
    school's, and the day is the whole point of the field.
    """
    if not isinstance(epoch_ms, (int, float)):
        return None
    return datetime.fromtimestamp(epoch_ms / 1000).date().isoformat()


def _held(offerings):
    """The offering currently held, i.e. the one with a roster seat."""
    for offering in offerings:
        if offering.get("rosterID"):
            return offering
    return None


def _offering(raw, held_id, changeable):
    taken = raw.get("currentStudents")
    cap = raw.get("maxStudents")
    left = cap - taken if isinstance(cap, int) and isinstance(taken, int) else None
    selected = raw.get("responsiveOfferingID") == held_id
    full = left is not None and left <= 0 and not selected
    return {
        "offering_id": raw.get("responsiveOfferingID"),
        "name": raw.get("responsiveOfferingName"),
        "description": raw.get("description") or None,
        "teacher": raw.get("teacherDisplay"),
        "room": raw.get("roomName") or None,
        "course_number": raw.get("courseNumber"),
        "section": raw.get("sectionNumber"),
        "seats_max": cap,
        "seats_taken": taken,
        "seats_left": left,
        "full": full,
        "selected": selected,
        "selectable": changeable and not full and not selected,
    }


def parse_sessions(data, include_empty=False):
    """Flatten the payload into one row per session, offerings sorted by name.

    `include_empty` keeps sessions the school has published with no offerings
    yet -- a real state (next week's block before teachers open their rooms),
    and one the portal hides. Hidden by default to match it.
    """
    sessions = []
    for raw in data if isinstance(data, list) else []:
        offerings = raw.get("offerings")
        offerings = offerings if isinstance(offerings, list) else []
        if not offerings and not include_empty:
            continue
        offerings = sorted(offerings, key=lambda o: (o.get("responsiveOfferingName")
                                                     or "").lower())
        held = _held(offerings)
        held_id = held.get("responsiveOfferingID") if held else None
        # A staff placement locks the session; so does a session with a single
        # offering already held, which is a fact rather than a choice.
        locked = bool(held and held.get("teacherRequest"))
        is_open = bool(raw.get("sessionOpen"))
        changeable = (is_open and bool(offerings) and not locked
                      and not (len(offerings) == 1 and offerings[0].get("rosterID")))
        sessions.append({
            "session_id": raw.get("responsiveSessionID"),
            "name": raw.get("sessionName"),
            "date": _date(raw.get("startDate")),
            "end_date": _date(raw.get("endDate")),
            "open": is_open,
            "changeable": changeable,
            "locked_by_staff": locked,
            "staff_reason": (held.get("reason") or None) if locked else None,
            "selected_offering_id": held_id,
            "selected": _offering(held, held_id, changeable) if held else None,
            "count": len(offerings),
            "offerings": [_offering(o, held_id, changeable) for o in offerings],
        })
    return sessions


def find_session(sessions, session_id=None):
    """A session by id, or the earliest one when no id is given."""
    if session_id is None:
        return sessions[0] if sessions else None
    for session in sessions:
        if str(session["session_id"]) == str(session_id):
            return session
    return None


def find_offering(session, offering_id):
    for offering in session["offerings"]:
        if str(offering["offering_id"]) == str(offering_id):
            return offering
    return None


def refusal(session, offering):
    """Why this selection must not be POSTed, or `None` when it may be.

    Checked before the write rather than after, because the portal answers an
    ineligible save with `success: false` and a message aimed at a person
    looking at the page, having already spent a round trip.
    """
    if offering is None:
        return "offering_not_in_session"
    if offering["selected"]:
        return "already_selected"
    if not session["open"]:
        return "session_closed"
    if session["locked_by_staff"]:
        return "placed_by_staff"
    if offering["full"]:
        return "offering_full"
    if not session["changeable"]:
        return "session_not_changeable"
    return None


def payload(ids, session_id, offering_id):
    """The update body, in the portal's own field names."""
    return {
        "personID": ids["personID"],
        "responsiveSessionID": int(session_id),
        "calendarID": ids["calendarID"],
        "responsiveOfferingID": int(offering_id),
    }
