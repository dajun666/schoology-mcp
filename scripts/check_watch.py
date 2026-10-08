"""Verification driver for the change-detection layer. Read-only.

Two modes:

    python scripts/check_watch.py --fixtures   # offline, reads dumps/ only
    python scripts/check_watch.py --live       # logs in and hits Schoology

The fixture mode is the important one: it runs the regressions that protect
against false alarms, and it needs no network, no login and no data in the
account. Run it after touching any parser or fingerprint.

Nothing here writes to disk (the live mode refreshes storage_state.json via the
normal login path, same as any tool call).
"""

import asyncio
import copy
import json
import pathlib
import re
import sys
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from schoology_mcp import gdocs, health, parsers, watch  # noqa: E402
from schoology_mcp.fingerprint import diff, fp, snapshot  # noqa: E402

DUMPS = pathlib.Path(__file__).resolve().parent.parent / "dumps"
FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "api"
BASE = "https://pausd.schoology.com"

_failures = []


def check(label, condition, extra=""):
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures.append(label)
    print(f"  [{status}] {label}{(' -- ' + str(extra)) if extra else ''}")


def section(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def _read(name):
    path = DUMPS / name
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _read_json(name):
    """A committed synthetic API fixture. Unlike `dumps/`, these are in the repo."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _grade_rows(html):
    # Deliberately the production flattener, not a lookalike: the fixture suite
    # must exercise the object the server actually builds.
    return watch.flatten_grades(parsers.parse_grades(html, BASE))


def run_api_fixtures():
    """The /v1 transforms, against committed synthetic JSON.

    Separate from `run_fixtures` because these fixtures live IN the repo: they
    need no `dumps/`, so this section still runs in a fresh clone where the
    HTML checks cannot. That is deliberate -- `dumps/` is a real student's
    gradebook and must never be committed, which historically left the suite
    unrunnable for anyone else.
    """
    from schoology_mcp import apiparse

    sections = _read_json("sections.json")
    grades_payload = _read_json("grades.json")

    section("A1. /v1 transforms keep the HTML output contract")
    api_courses = apiparse.courses(sections, BASE)
    check("courses parsed", len(api_courses) == 2, f"{len(api_courses)} courses")
    # Exactly the keys parsers.parse_courses emitted. A renamed key does not
    # raise anywhere -- it just makes a course untitled in every saved note.
    check("course rows carry {title, url}",
          all(set(c) == {"title", "url"} for c in api_courses), api_courses[:1])
    check("course url points at the roster section id",
          api_courses[0]["url"] == f"{BASE}/course/1000000001", api_courses[0]["url"])

    # Attribution is by the id we ASKED for, never the one the payload reports:
    # some real graded courses come back under a different section id.
    grades_by_sid = {"1000000001": grades_payload}
    # From /v1/sections/<sid>/grade_items, not /assignments -- the latter omits
    # hand-made grade columns, which then render as "Assignment <id>".
    assignments_by_sid = {"1000000001": {"assignment": [
        {"id": 2000000011, "title": "Widget Report", "due": "2026-09-04 23:59:00",
         "grading_category": "4000001"},
        {"id": 2000000012, "title": "Sprocket Sketch", "due": "",
         "grading_category": "4000001"},
    ]}}
    categories_by_sid = {"1000000001": {"grading_category": [
        {"id": 4000001, "title": "Homework", "weight": 30},
    ]}}
    api_grades = apiparse.grades(sections, grades_by_sid, assignments_by_sid,
                                 categories_by_sid, BASE)
    # Both sections are listed even though only one has scores: the gradebook
    # page lists every enrolled course with "N/A" against the ungraded ones,
    # and dropping them would answer "which courses have grades" instead of
    # "what are my grades".
    check("every enrolled section is listed, graded or not",
          len(api_grades) == 2, f"{len(api_grades)}")
    ungraded = next(c for c in api_grades if c["id"] == "1000000002")
    check("an ungraded course has course_grade None",
          ungraded["course_grade"] is None, ungraded["course_grade"])
    check("an ungraded course still carries its course row",
          [r["type"] for r in ungraded["rows"]] == ["course"],
          [r["type"] for r in ungraded["rows"]])

    required_course = {"id", "title", "course_grade", "rows", "fp"}
    check("course objects carry the parse_grades keys",
          all(required_course <= set(c) for c in api_grades))

    rows = watch.flatten_grades(api_grades)
    required_row = {"id", "uid", "parent_id", "type", "title", "percentage_contrib",
                    "due", "grade", "comment", "submission", "assignment_url",
                    "fp", "fp_meta", "course", "url"}
    missing = [sorted(required_row - set(r)) for r in rows if not required_row <= set(r)]
    check("every grade row is shaped like the HTML one", not missing, missing[:1])
    check("row uids are unique", len({r["uid"] for r in rows}) == len(rows),
          f"{len(rows)} rows")
    check("row types stay in the known vocabulary",
          {r["type"] for r in rows} <= {"course", "period", "category", "item"},
          {r["type"] for r in rows})
    check("no row is ever labelled not_submitted",
          all(r["submission"] != "not_submitted" for r in rows))

    section("A2. Ungraded is not zero, and titles survive the join")
    items = {r["id"]: r for r in rows if r["type"] == "item"}
    check("graded item keeps its score",
          items["2000000011"]["grade"]["numeric"] == "92", items["2000000011"]["grade"])
    # The single most damaging silent bug available here: an ungraded row
    # rendered as 0 is indistinguishable from a real zero to the alerting layer.
    check("ungraded item has grade None, not 0",
          items["2000000012"]["grade"] is None, items["2000000012"]["grade"])
    check("assignment titles came from the assignments payload",
          items["2000000011"]["title"] == "Widget Report", items["2000000011"]["title"])

    # /v1/.../grades gives ids as ints while /sections gives strings; if the
    # join stopped normalizing, titles would silently become placeholders.
    check("int/str id mismatch does not break the title join",
          not items["2000000011"]["title"].startswith("Assignment "))

    section("A3. Timestamp drift must NOT look like a change")
    # `timestamp` is when a score was entered -- a teacher fixing a typo
    # refreshes it. In `fp` that would be a false alert every single run.
    drifted = copy.deepcopy(grades_payload)
    for entry in drifted["section"]:
        for period in entry["period"]:
            for item in period["assignment"]:
                item["timestamp"] = (item.get("timestamp") or 0) + 86_400
    check("fixture actually mutated", drifted != grades_payload)
    before = snapshot(rows, id_key="uid")
    after = snapshot(
        watch.flatten_grades(
            apiparse.grades(sections, {"1000000001": drifted}, assignments_by_sid,
                            categories_by_sid, BASE)
        ),
        id_key="uid",
    )
    check("grades digest unchanged after timestamp drifts",
          before["digest"] == after["digest"], before["digest"])

    section("A4. Determinism and degraded inputs")
    again = snapshot(
        watch.flatten_grades(
            apiparse.grades(sections, grades_by_sid, assignments_by_sid,
                            categories_by_sid, BASE)
        ),
        id_key="uid",
    )
    check("digest reproducible", before["digest"] == again["digest"], again["digest"])
    # A missing assignments payload must not drop rows: an unlabelled grade
    # change is still worth alerting on.
    bare = apiparse.grades(sections, grades_by_sid, None, None, BASE)
    bare_rows = watch.flatten_grades(bare)
    check("rows survive a missing assignments payload",
          len(bare_rows) == len(rows) - 1, f"{len(bare_rows)} vs {len(rows)}")
    check("without categories the items sit flat under their period",
          all(r["type"] != "category" for r in bare_rows))
    check("empty inputs yield empty output, not an exception",
          apiparse.courses({}, BASE) == [] and apiparse.grades({}, {}, {}, {}, BASE) == [])

    section("A5. Grading categories rebuild the gradebook's middle tier")
    cat_rows = [r for r in rows if r["type"] == "category"]
    check("one category row per period", len(cat_rows) == 1, len(cat_rows))
    check("the category carries its weight",
          cat_rows[0]["percentage_contrib"] == "(30%)",
          cat_rows[0]["percentage_contrib"])
    # The API reports no per-category score, only the weight. None beats a
    # computed guess that would then be diffed as if Schoology had said it.
    check("category rows carry no invented grade", cat_rows[0]["grade"] is None)
    categorized = [r for r in rows if r["type"] == "item"
                   and r["parent_id"] == cat_rows[0]["id"]]
    check("items with a known category hang off it", len(categorized) == 2,
          len(categorized))
    # The third item's row metadata carries no category, so it falls back to
    # the grading period. Degrading to a flatter tree beats dropping the row.
    orphan = next(r for r in rows if r["type"] == "item" and r not in categorized)
    check("an item with no category falls back to its period",
          orphan["parent_id"] == "1000000009.p2000001", orphan["parent_id"])


def run_event_fixtures():
    """Events and folder items -- the two transforms that retired a blind spot."""
    from schoology_mcp import apiparse

    sections = _read_json("sections.json")

    section("A6. Events carry course attribution and collapse duplicates")
    rows = apiparse.events(_read_json("events.json"), sections, BASE)
    # Teachers do create the same deadline twice, which is how "Widget Reading Due"
    # reached the upcoming list twice. Ids differ, so id cannot be the key.
    check("duplicate occurrences collapse", len(rows) == 4, f"{len(rows)} rows")
    by_title = {r["title"]: r for r in rows}
    check("course attribution comes from section_id, not the title",
          by_title["Widget Reading Due"]["course"].startswith("Intro to Widgets"),
          by_title["Widget Reading Due"]["course"])
    # The whole point: work posted as a calendar entry is still work, and it
    # never appears in the upcoming widget or the gradebook.
    check("a type=event row is still surfaced",
          by_title["Widget Reading Due"]["kind"] == "event")
    # Event ids and assignment ids come from disjoint sequences --
    # /assignment/<event_id> is a 403 -- so an assignment link must be built
    # from the event's own assignment_id, never from its id. The wrong URL is
    # entirely plausible-looking, which is what makes it worth a check.
    check("assignment links use assignment_id, not the event id",
          by_title["Widget Lab Writeup"]["url"].endswith("/assignment/4000000001"),
          by_title["Widget Lab Writeup"]["url"])
    check("rows with no assignment behind them link to /event/",
          by_title["Widget Reading Due"]["url"].endswith("/event/5000000002"),
          by_title["Widget Reading Due"]["url"])
    check("assessments count as assignments",
          by_title["Sprocket Quiz"]["is_assignment"] is True)
    check("a school-realm event with no section does not break the join",
          by_title["Smith & Jones Guest Talk"]["course"] is None)
    # /v1 HTML-escapes titles. Most contain nothing to escape, so this slips
    # through until an "&" lands in a course name and gets saved that way.
    check("titles are HTML-unescaped", "&amp;" not in " ".join(by_title))
    check("rows are sorted by start", [r["start"] for r in rows]
          == sorted(r["start"] for r in rows))

    section("A7. Folder items keep the materials contract")
    folder = apiparse.folder_items(_read_json("folder.json"), "9000001", BASE)
    check("all three items parsed", len(folder) == 3, len(folder))
    required = {"type", "title", "url", "preview", "folder", "folder_id"}
    check("rows carry the parse_course_materials keys",
          all(required <= set(f) for f in folder))
    sub = next(f for f in folder if f["type"] == "folder")
    check("folder rows expose the id the walk recurses on",
          sub["folder_id"] == "6000000001", sub["folder_id"])
    check("folder urls stay browsable (?f=<id>)",
          sub["url"] == f"{BASE}/course/9000001/materials?f=6000000001", sub["url"])
    check("folder titles are unescaped too", sub["title"] == "Unit 1 & Intro",
          sub["title"])
    section("A8. Attachment links must point at a fetchable host")
    # The API hands back https://api.<host>/v1/attachment/..., whose certificate
    # covers *.schoology.com and so does not match a three-label name. Neither
    # httpx nor Playwright will touch it -- the URL as given is simply dead.
    given = "https://api.example.schoology.com/v1/attachment/123/source/abc"
    check("the api-host form is rewritten onto the main host",
          apiparse.attachment_url(given)
          == "https://example.schoology.com/attachment/123/source/abc",
          apiparse.attachment_url(given))
    already = "https://example.schoology.com/attachment/123/source/abc.pdf"
    check("an already-usable url is left alone",
          apiparse.attachment_url(already) == already)
    check("a non-attachment url is left alone",
          apiparse.attachment_url("https://docs.google.com/document/d/x/edit")
          == "https://docs.google.com/document/d/x/edit")
    check("empty input stays empty", apiparse.attachment_url(None) is None)

    section("A9. The change-detection window must not move with the date")
    # The single most effective way to break this feature is to fingerprint a
    # window anchored to today: it shifts every midnight, the digest with it,
    # and the user gets one "calendar changed" alert per day forever. This is
    # the same reason the iCal path hashed the entire feed.
    import datetime as _dt
    windows = {apiparse.school_year_window(_dt.date.fromisoformat(d))
               for d in ("2026-09-01", "2026-10-15", "2026-12-31",
                         "2027-01-15", "2027-05-02", "2027-07-31")}
    check("every date inside one school year gives the same window",
          len(windows) == 1, windows)
    rolls = apiparse.school_year_window(_dt.date.fromisoformat("2027-08-01"))
    check("it rolls over at the start of August, exactly once a year",
          rolls not in windows, rolls)

    check("an unknown type degrades rather than raising",
          apiparse.folder_items({"folder-item": [{"id": 1, "title": "x",
                                                  "type": "wat"}]},
                                "9000001", BASE)[0]["type"] == "unknown")


def run_responsive_fixtures():
    """The flex-period picker: eligibility must match the portal's own rules.

    Every rule here is one the portal enforces client-side. Getting one wrong
    does not raise -- it produces a POST the SIS answers `success: false`, or
    worse, hides an offering the student could actually have taken.
    """
    from datetime import datetime

    from schoology_mcp import responsive

    section("A10. The flex period: seats, locks and refusals")
    sessions = responsive.parse_sessions(_read_json("responsive_schedule.json"))

    check("a session with no offerings is hidden, as in the portal",
          [s["session_id"] for s in sessions] == [9001, 9002, 9003, 9005],
          [s["session_id"] for s in sessions])
    check("include_empty brings it back",
          len(responsive.parse_sessions(_read_json("responsive_schedule.json"),
                                        include_empty=True)) == 5)

    first = sessions[0]
    check("offerings are sorted by name, not by id",
          [o["name"] for o in first["offerings"]]
          == ["Alpha Room", "Middle Room", "Zeta Room"])

    held = first["selected"]
    check("the seat with a rosterID is the one held", held["offering_id"] == 70001)
    # The held seat is at capacity. Counting it as full would make the student's
    # own row unselectable and, worse, read as "you cannot stay where you are".
    check("a full offering already held is not 'full'",
          held["seats_left"] == 0 and held["full"] is False)
    by_id = {o["offering_id"]: o for o in first["offerings"]}
    check("a full offering not held is full and unselectable",
          by_id[70003]["full"] and not by_id[70003]["selectable"])
    check("a seat with room is selectable", by_id[70002]["selectable"])
    check("the held seat is not offered as a change", not held["selectable"])

    check("re-picking the seat already held is refused",
          responsive.refusal(first, by_id[70001]) == "already_selected")
    check("a full offering is refused",
          responsive.refusal(first, by_id[70003]) == "offering_full")
    check("a free offering is accepted",
          responsive.refusal(first, by_id[70002]) is None)
    check("an offering from another session is refused",
          responsive.refusal(first, None) == "offering_not_in_session")

    single = responsive.find_session(sessions, 9002)
    check("one offering, already held, is not a choice",
          single["changeable"] is False)

    locked = responsive.find_session(sessions, 9003)
    check("a staff placement locks the session",
          locked["locked_by_staff"] and locked["staff_reason"] == "Make-up test")
    check("and refuses every other offering in it",
          responsive.refusal(locked, responsive.find_offering(locked, 70022))
          == "placed_by_staff")

    closed = responsive.find_session(sessions, 9005)
    check("a closed session refuses a selection",
          responsive.refusal(closed, responsive.find_offering(closed, 70031))
          == "session_closed")

    # `startDate` is midnight in the *school's* timezone. Read as UTC it lands
    # on the previous day for anyone west of it, which silently books the wrong
    # date. Built from a local datetime here so the check is timezone-neutral.
    midnight = int(datetime(2026, 1, 5).timestamp() * 1000)
    dated = responsive.parse_sessions([{"responsiveSessionID": 9101,
                                        "startDate": midnight,
                                        "sessionOpen": True,
                                        "offerings": [{"responsiveOfferingID": 70041}]}])
    check("local midnight resolves to the day it is midnight on",
          dated[0]["date"] == "2026-01-05", dated[0]["date"])

    roster = [{"courseName": "x"},
              {"personID": 1000000001, "calendarID": 2000000001,
               "structureID": 3000000001, "courseName": "y"}]
    ids = responsive.enrollment(roster)
    check("the enrolment ids come off the roster, not the student record",
          ids == {"personID": 1000000001, "calendarID": 2000000001,
                  "structureID": 3000000001})
    check("an empty roster is 'no enrolment', not a crash",
          responsive.enrollment([]) is None and responsive.enrollment(None) is None)
    check("the update body uses the portal's field names",
          responsive.payload(ids, "9001", "70002") == {
              "personID": 1000000001, "calendarID": 2000000001,
              "responsiveSessionID": 9001, "responsiveOfferingID": 70002})


def run_redirect_checks():
    """A11 -- the `/link?path=` redirector. Pure string work, no fixture needed."""
    section("A11. External links survive Schoology's redirector")

    doc = "https://docs.google.com/document/d/1AbCdEf/edit"
    quoted = "https://pausd.schoology.com/link?a=&path=" + urllib.parse.quote(doc, safe="")
    check("the target survives percent-decoding intact",
          parsers.link_redirect_target(quoted.replace(BASE, "")) == doc)
    check("an absolute redirector parses the same as a relative one",
          parsers.link_redirect_target(quoted) == doc)

    # The distinction the three-way return exists for: a material URL must fall
    # through to the normal dispatcher, while a redirector with nothing in it is
    # a broken URL the caller has to be told about.
    check("a material URL is not claimed as a redirector",
          parsers.link_redirect_target("/course/1000000001/materials/page/2000000011") is None)
    check("an empty redirector reports itself as empty, not as 'not mine'",
          parsers.link_redirect_target("/link?a=") == "")

    # `a=` is empty in every redirector Schoology emits, and the target sits in
    # the second parameter -- parsing by position instead of by name would read
    # the wrong one and hand back "".
    check("the target is read by name, not by position",
          parsers.link_redirect_target("/link?a=&path=https%3A%2F%2Fexample.org%2Fx")
          == "https://example.org/x")

    # A Drive link is what makes this worth resolving: `get_material` exports it
    # only when `gdocs` recognises the host in the decoded target.
    check("a decoded Drive target is recognised for export",
          gdocs.is_drive_url(parsers.link_redirect_target(quoted)))
    check("a non-Drive target is not sent to the exporter",
          not gdocs.is_drive_url(
              parsers.link_redirect_target("/link?a=&path=https%3A%2F%2Fexample.org%2Fx")))


def run_fixtures():
    run_api_fixtures()
    run_event_fixtures()
    run_responsive_fixtures()
    run_redirect_checks()

    grades_html = _read("grades.html")
    home_html = _read("home.html")
    messages_html = _read("messages.html")

    missing = [n for n, h in (("grades.html", grades_html), ("home.html", home_html))
               if h is None]
    if missing:
        sys.exit(
            f"Missing fixtures {missing} in {DUMPS}. Run "
            "`python scripts/login_check.py` first."
        )

    section("1. Parsers produce ids and fingerprints")
    rows = _grade_rows(grades_html)
    courses = parsers.parse_grades(grades_html, BASE)
    check("every course has an id", all(c["id"] for c in courses))
    check("row uids are unique gradebook-wide",
          len({r["uid"] for r in rows}) == len(rows), f"{len(rows)} rows")
    check("no row is ever labelled not_submitted",
          all(r["submission"] != "not_submitted" for r in rows))
    upcoming = parsers.parse_upcoming_assignments(home_html, BASE)
    posts = parsers.parse_recent_posts(home_html, BASE, limit=50)
    check("upcoming items carry ids", all(u["id"] for u in upcoming), f"{len(upcoming)} items")
    check("posts carry ids", all(p["id"] for p in posts), f"{len(posts)} posts")

    section("2. Determinism -- identical input must give identical digests")
    for label, items_a, items_b in (
        ("grades", _grade_rows(grades_html), _grade_rows(grades_html)),
        ("upcoming", parsers.parse_upcoming_assignments(home_html, BASE),
         parsers.parse_upcoming_assignments(home_html, BASE)),
        ("posts", parsers.parse_recent_posts(home_html, BASE),
         parsers.parse_recent_posts(home_html, BASE)),
    ):
        key = "uid" if label == "grades" else "id"
        a, b = snapshot(items_a, id_key=key), snapshot(items_b, id_key=key)
        check(f"{label} digest reproducible", a["digest"] == b["digest"], a["digest"])

    section("3. Time drift must NOT look like a change")
    # "80 days overdue" is recomputed daily. If it reached a fingerprint every
    # overdue assignment would alert once per day, forever.
    drifted = re.sub(r"(\d+) days overdue",
                     lambda m: f"{int(m.group(1)) + 1} days overdue", home_html)
    check("fixture actually mutated", drifted != home_html)
    before = snapshot(parsers.parse_upcoming_assignments(home_html, BASE))
    after = snapshot(parsers.parse_upcoming_assignments(drifted, BASE))
    check("upcoming digest unchanged after overdue text drifts",
          before["digest"] == after["digest"])

    section("4. Empty vs broken")
    intact = health.evaluate(grades_html, anchors=parsers.GRADES_ANCHORS,
                             empty_markers=parsers.GRADES_EMPTY_MARKERS,
                             item_count=len(courses), prev_count=None)
    check("populated page -> ok", intact["status"] == "ok", intact["status"])

    no_items = grades_html.replace("s-grades-course-item", "s-grades-course-itemXX")
    collapsed = health.evaluate(no_items, anchors=("ul.s-grades-course-list",),
                                empty_markers=parsers.GRADES_EMPTY_MARKERS,
                                item_count=0, prev_count=431)
    check("0 items but baseline had many -> suspect",
          collapsed["status"] == "suspect", collapsed["status"])
    check("suspect is not reportable", not health.reportable(collapsed))

    no_anchor = no_items.replace("s-grades-course-list", "s-grades-course-listXX")
    drift = health.evaluate(no_anchor, anchors=("ul.s-grades-course-list",),
                            empty_markers=(), item_count=0, prev_count=431)
    check("skeleton gone -> error/markup_drift",
          drift["status"] == "error" and drift["warning"] == "markup_drift")

    fresh = health.evaluate(no_items, anchors=("ul.s-grades-course-list",),
                            empty_markers=(), item_count=0, prev_count=0)
    check("0 items and none before -> quiet empty", fresh["status"] == "empty")

    check("i18n bundle text does not fake an empty state",
          intact["status"] == "ok",
          "empty markers are matched against visible text only")

    section("5. A failed source never poisons the baseline")
    prev = {"value": snapshot(rows, id_key="uid")}
    payload, entry, alerts = watch.evaluate_source(
        "grades", items=[], health=collapsed, prev_entry=prev,
        id_key="uid", meta_key="fp_meta")
    check("no alerts from an unhealthy source", alerts == [])
    check("baseline echoed back byte-identical", entry == prev)
    check("marked stale", payload["health"].get("stale") is True)

    section("6. First run is silent")
    first_payload, first_entry, first_alerts = watch.evaluate_source(
        "grades", items=rows, health=intact, prev_entry=None,
        id_key="uid", meta_key="fp_meta")
    check("no baseline -> zero alerts", first_alerts == [])
    check("first_run flagged", first_payload["first_run"] is True)
    check("but a baseline is returned to store", bool(first_entry["value"]["items"]))
    check("diff() reports nothing as added on first run",
          diff(None, snapshot(rows, id_key="uid"))["added"] == [])

    section("7. Real changes are classified correctly")
    mutated = copy.deepcopy(rows)
    target = next(r for r in mutated if r["type"] == "item" and r["grade"])
    target["grade"] = {**target["grade"], "raw": "99 / 100"}
    target["fp"] = fp(target["uid"], target["grade"], target["comment"],
                      target["submission"])
    _, _, grade_alerts = watch.evaluate_source(
        "grades", items=mutated, health=intact, prev_entry=prev,
        id_key="uid", meta_key="fp_meta")
    check("a changed score yields one grade_changed alert",
          len(grade_alerts) == 1 and grade_alerts[0]["kind"] == "grade_changed",
          [a["kind"] for a in grade_alerts])
    check("classified as a grade change, not metadata",
          grade_alerts[0].get("changed_fields") == ["grade"],
          grade_alerts[0].get("changed_fields"))

    cosmetic = copy.deepcopy(rows)
    target2 = next(r for r in cosmetic if r["type"] == "item")
    target2["title"] = (target2["title"] or "") + " (typo fixed)"
    target2["fp_meta"] = fp(target2["uid"], target2["title"], target2["due"],
                            target2["percentage_contrib"], target2["parent_id"])
    _, _, cosmetic_alerts = watch.evaluate_source(
        "grades", items=cosmetic, health=intact, prev_entry=prev,
        id_key="uid", meta_key="fp_meta")
    check("a title-only edit raises no grade alert", cosmetic_alerts == [],
          [a["kind"] for a in cosmetic_alerts])

    section("8. Bulk turnover collapses to one alert")
    # Drop a fixed three quarters of the rows rather than "every item row".
    # The threshold is max(10, 50% of the previous total), and the item/period
    # ratio in a real gradebook moves with the school year -- in an early-term
    # dump item rows were only 22 of 124 (18%), which never reached it, so this
    # check failed for a reason that had nothing to do with the code under test.
    survivors = rows[:len(rows) // 4]
    bulk_health = health.evaluate(grades_html, anchors=parsers.GRADES_ANCHORS,
                                  empty_markers=(), item_count=len(survivors),
                                  prev_count=len(rows))
    _, _, bulk_alerts = watch.evaluate_source(
        "grades", items=survivors, health=bulk_health, prev_entry=prev,
        id_key="uid", meta_key="fp_meta")
    check("mass removal -> a single aggregate alert",
          len(bulk_alerts) == 1 and bulk_alerts[0]["kind"] == "grades_bulk_removed",
          [a["kind"] for a in bulk_alerts])

    section("9. Expiry is never news (outside grades)")
    up_prev = {"value": snapshot(upcoming)}
    up_health = health.evaluate(home_html, anchors=parsers.UPCOMING_ANCHORS,
                                empty_markers=parsers.UPCOMING_EMPTY_MARKERS,
                                item_count=max(0, len(upcoming) - 1),
                                prev_count=len(upcoming))
    _, _, up_alerts = watch.evaluate_source(
        "upcoming", items=upcoming[1:], health=up_health, prev_entry=up_prev)
    check("an assignment leaving the list raises no alert", up_alerts == [],
          [a["kind"] for a in up_alerts])

    if messages_html:
        section("10. Messages: unread must not enter the fingerprint")
        a = parsers.parse_messages(messages_html, BASE)
        b = parsers.parse_messages(messages_html, BASE)
        for m in b:
            m["unread"] = not m.get("unread")
        check("flipping unread leaves every fp identical",
              all(x["fp"] == y["fp"] for x, y in zip(a, b)), f"{len(a)} messages")
    else:
        print("\n(skipping message checks -- dumps/messages.html not present)")


async def run_live():
    import server  # imported lazily: constructs a browser client

    section("LIVE 1. Health")
    info = await server.get_health()
    print(f"  logged_in={info['logged_in']} status={info['health']['status']} "
          f"elapsed={info['elapsed_ms']}ms "
          f"cookie_ttl={info['session'].get('seconds_to_expiry')}s")
    check("logged in", info["logged_in"] is True)
    check("home page skeleton found", info["health"]["anchor_found"] is True)

    section("LIVE 2. First run is silent, second run is quiet")
    # verbose=True so health comes back as a full diagnostic dict; the default
    # terse form collapses it to a status string to save the caller tokens.
    first = await server.check_updates(verbose=True)
    print("  counts:", {n: p.get("count") for n, p in first["sources"].items()})
    check("first run raises no alerts", first["alerts"] == [])
    for name, payload in first["sources"].items():
        detail = payload.get("health")
        check(f"{name} is not an error", payload["status"] != "error",
              detail.get("error") if isinstance(detail, dict) else detail)

    second = await server.check_updates(baseline=first["baseline"])
    check("replaying the baseline reports no change", second["changed"] == [],
          second["changed"])
    check("and raises no alerts", second["alerts"] == [], len(second["alerts"]))
    for name in first["sources"]:
        check(f"{name} digest stable across runs",
              first["sources"][name].get("digest") == second["sources"][name].get("digest"))
    # A quiet run must not re-send the baseline -- that is the whole token saving.
    check("quiet run omits the baseline", second.get("baseline_unchanged") is True
          and "baseline" not in second,
          f"baseline_unchanged={second.get('baseline_unchanged')} "
          f"baseline_present={'baseline' in second}")

    section("LIVE 3. Unrequested sources pass through untouched")
    partial = await server.check_updates(baseline=first["baseline"], sources=["messages"])
    # Nothing moved and unrequested sources were carried over verbatim, so the
    # baseline is unchanged -- which is itself the proof that grades survived.
    if "baseline" in partial:
        check("grades baseline preserved",
              partial["baseline"]["sources"].get("grades")
              == first["baseline"]["sources"].get("grades"))
    else:
        check("baseline untouched by a messages-only run",
              partial.get("baseline_unchanged") is True)

    section("LIVE 4. Listing messages does not clear unread")
    before = (await server.get_messages(folder="inbox", limit=1))["unread_count"]
    await server.get_messages(folder="inbox", limit=25)
    after = (await server.get_messages(folder="inbox", limit=1))["unread_count"]
    check("unread count unchanged by listing", before == after, f"{before} -> {after}")
    if before == 0:
        print("      NOTE: inbox has 0 unread, so this cannot distinguish "
              "'listing is safe' from 'nothing to clear'. Re-run when unread > 0.")

    await server.client.close()


def main():
    live = "--live" in sys.argv
    fixtures = "--fixtures" in sys.argv or not live

    if fixtures:
        run_fixtures()
    if live:
        asyncio.run(run_live())

    print(f"\n{'=' * 78}")
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for name in _failures:
            print(f"  - {name}")
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
