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
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from schoology_mcp import health, parsers, watch  # noqa: E402
from schoology_mcp.fingerprint import diff, fp, snapshot  # noqa: E402

DUMPS = pathlib.Path(__file__).resolve().parent.parent / "dumps"
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


def _grade_rows(html):
    # Deliberately the production flattener, not a lookalike: the fixture suite
    # must exercise the object the server actually builds.
    return watch.flatten_grades(parsers.parse_grades(html, BASE))


def run_fixtures():
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
    survivors = [r for r in rows if r["type"] != "item"]
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
