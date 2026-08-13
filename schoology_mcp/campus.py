"""Infinite Campus (the district SIS) -- schedule and roster.

Schoology is the learning platform; Infinite Campus is the system of record for
enrolment. It knows the things Schoology never shows: which room a class is in,
what period it falls in, and the bell times.

The student portal is a single-page app, so there is no server-rendered HTML
worth scraping -- but it is backed by a plain JSON API that the authenticated
browser context can call directly. That makes this module a pure transform over
JSON rather than another set of fragile CSS selectors:

    GET /campus/resources/portal/roster   -> one entry per enrolled section
    GET /campus/resources/portal/grades   -> posted grades by term

Access rides the same ClassLink SSO as Schoology (a different app tile), so no
separate credentials exist.

**Privacy:** the portal also exposes `/campus/resources/portal/students`, which
returns the student's legal name, district student number and state ID. Nothing
here reads it, and nothing here should -- the roster carries everything the
schedule needs and none of that identity payload.

Disabled unless `CAMPUS_ENABLED=true`: the host and the tile name are
district-specific, so for anyone who forked this repo the feature would only
produce confusing failures.
"""

ROSTER_PATH = "/campus/resources/portal/roster"
GRADES_PATH = "/campus/resources/portal/grades"

# Weekday-independent: the portal reports each section's placement within a
# period schedule, not per calendar day.
_PLACEMENT_FIELDS = ("periodName", "startTime", "endTime", "roomName",
                     "teacherDisplay", "termName")


def _placements(entry):
    value = entry.get("sectionPlacements")
    return value if isinstance(value, list) else []


# The portal lists a placement per *bell schedule*, not per class. A single
# course carries "Full" (the regular day) plus one for each variant day and
# one-off date -- Biology alone has six, and its Thursday block runs 12:52-14:22
# instead of 10:57-11:42. Expanding all of them turns 13 classes into 54 rows of
# near-duplicates, so one schedule is picked and the rest are available on
# request.
DEFAULT_SCHEDULE = "Full"


def schedules_in(data):
    """Distinct bell-schedule names in the roster, most common first."""
    counts = {}
    for entry in data if isinstance(data, list) else []:
        for placement in _placements(entry):
            name = placement.get("periodScheduleName")
            if name:
                counts[name] = counts.get(name, 0) + 1
    return [n for n, _ in sorted(counts.items(), key=lambda kv: -kv[1])]


def _row(entry, placement):
    row = {
        "course": entry.get("courseName"),
        "course_number": entry.get("courseNumber"),
        "section": entry.get("sectionNumber"),
        "school": entry.get("schoolName"),
        # Also on the entry, but a placement may override it.
        "room": entry.get("roomName"),
        "teacher": entry.get("teacherDisplay"),
        "period": None, "start_time": None, "end_time": None, "term": None,
    }
    if placement is None:
        return row
    for field, key in zip(
        _PLACEMENT_FIELDS,
        ("period", "start_time", "end_time", "room", "teacher", "term"),
    ):
        value = placement.get(field)
        if value is not None:
            row[key] = value
    term_info = placement.get("term") or {}
    row["term_start"] = term_info.get("startDate")
    row["term_end"] = term_info.get("endDate")
    row["schedule_name"] = placement.get("periodScheduleName")
    return row


def parse_roster(data, term: str | None = None, schedule: str | None = DEFAULT_SCHEDULE):
    """One row per class per term: period, times, room, teacher.

    `schedule` picks which bell schedule to report ("Full" = the regular day;
    "M"/"T"/"W"/"R"/"F" are day-specific variants). `None` returns every
    placement, which is what you want to see a block day's real times.

    A course with no placement in the requested schedule is still returned --
    using whatever placement it does have -- because silently dropping a class
    from a schedule is worse than showing it with an unusual bell time. Its
    `schedule_name` says which one was used.

    `term` filters by term name ("S1"/"S2"); a year-long course is placed in
    both, so without it each class appears twice.
    """
    if not isinstance(data, list):
        return []

    rows = []
    for entry in data:
        placements = _placements(entry)
        if not placements:
            rows.append(_row(entry, None))
            continue

        if schedule is None:
            rows.extend(_row(entry, p) for p in placements)
            continue

        # Keep one placement per term: the requested schedule when the course
        # has it, else that term's first.
        by_term: dict = {}
        for placement in placements:
            key = placement.get("termName")
            current = by_term.get(key)
            if current is None or (
                placement.get("periodScheduleName") == schedule
                and current.get("periodScheduleName") != schedule
            ):
                by_term[key] = placement
        rows.extend(_row(entry, p) for p in by_term.values())

    if term:
        wanted = term.strip().lower()
        rows = [r for r in rows if (r.get("term") or "").lower() == wanted]

    # Order by period as a number when it is one -- "10" must not sort before
    # "2", and the portal returns period names as strings.
    def sort_key(row):
        period = row.get("period")
        try:
            return (0, float(period), "")
        except (TypeError, ValueError):
            return (1, 0.0, str(period or ""))

    rows.sort(key=sort_key)
    return rows


def terms_in(rows):
    """Distinct term names present, in the order they first appear."""
    seen = []
    for row in rows:
        term = row.get("term")
        if term and term not in seen:
            seen.append(term)
    return seen
