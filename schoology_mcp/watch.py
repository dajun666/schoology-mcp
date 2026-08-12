"""Change detection over already-fetched data. Pure -- no I/O, no Playwright.

`server.py` fetches and parses; this module decides what changed and what is
worth telling a human about. Keeping it pure means the whole thing can be
exercised against `dumps/` with no network and no login.

The contract with the caller (see `fingerprint.py`): the server remembers
nothing between runs. The caller stores the `baseline` this module emits and
hands it back next time. Everything here is written so that replaying the same
baseline twice produces the identical result -- crashes and irregular schedules
degrade to a duplicate notification, never a missed one.

Three rules exist purely to prevent false alarms, and each one has a specific
failure it is defending against:

1. **A failed source never advances the baseline.** When health says `error` or
   `suspect`, the caller's previous entry is echoed back untouched and marked
   stale. Without this, one bad scrape blanks the cursor and the *next* run
   reports the entire gradebook as new.
2. **First run is always silent.** No baseline means no way to know what is
   new, so nothing is announced -- only a baseline is handed back.
3. **`removed` only alerts for grades.** Assignments and calendar events leave
   their lists by expiring; alerting on that would fire every single day.
"""

from . import health as health_mod
from .fingerprint import diff, snapshot

# Sources whose disappearance is real news. Everywhere else, items leave the
# list because they expired, which is not an event worth waking someone for.
_ALERT_ON_REMOVED = frozenset({"grades"})

DEFAULT_SOURCES = ("grades", "upcoming", "messages", "calendar")
ALL_SOURCES = ("grades", "upcoming", "messages", "calendar", "posts")

# When this much of a source turns over at once, emit ONE aggregate alert
# instead of hundreds. `health.evaluate` catches a source collapsing to zero,
# but a *partial* selector break (item rows stop parsing while course rows
# still do) looks like a legitimate mass removal, and so does a real term
# rollover. The two are indistinguishable from the data, so rather than guess
# -- or spam 380 notifications -- say plainly that a lot moved at once and let
# a human look.
_BULK_FRACTION = 0.5
_BULK_MINIMUM = 10


def flatten_grades(courses):
    """Course tree -> one flat row list carrying course title and item URL.

    Pure, and shared by the server and the fixture suite: when each kept its own
    copy they drifted (the fixture's omitted `url`, so grade alerts' links were
    never actually exercised by the tests that claim to cover them).
    """
    rows = []
    for course in courses:
        for row in course["rows"]:
            rows.append({
                **row,
                "course": course["title"],
                "url": row.get("assignment_url"),
            })
    return rows


def _trim(item, keys):
    return {k: item.get(k) for k in keys if item.get(k) is not None}


# Fields worth showing per source when something changed. Deliberately narrow:
# a digest that dumps whole objects stops being a digest.
_DETAIL_KEYS = {
    "grades": ("uid", "title", "course", "type", "due", "grade", "comment",
               "submission", "assignment_url"),
    "upcoming": ("id", "title", "course", "due", "due_iso", "url"),
    "messages": ("id", "subject", "sender", "date_text", "preview", "unread", "url"),
    "calendar": ("id", "summary", "start", "end", "all_day", "url"),
    "posts": ("id", "author", "posted_to", "timestamp", "text", "url"),
}

_ALERT_KIND = {
    ("grades", "added"): "grade_item_added",
    ("grades", "changed"): "grade_changed",
    ("grades", "removed"): "grade_removed",
    ("upcoming", "added"): "new_assignment",
    ("upcoming", "changed"): "assignment_updated",
    ("messages", "added"): "new_message",
    ("messages", "changed"): "message_updated",
    ("calendar", "added"): "new_event",
    ("calendar", "changed"): "event_updated",
    ("posts", "added"): "new_post",
    ("posts", "changed"): "post_updated",
}


def _label(source, item):
    if source == "grades":
        title = item.get("title") or item.get("uid")
        course = item.get("course")
        return f"{course}: {title}" if course else str(title)
    if source == "messages":
        return item.get("subject") or "(no subject)"
    if source == "calendar":
        return item.get("summary") or "(untitled event)"
    return item.get("title") or item.get("text") or "(untitled)"


def _detail_value(source, item):
    if source == "grades":
        grade = item.get("grade") or {}
        return grade.get("raw") or item.get("submission")
    if source == "upcoming":
        return item.get("due_iso") or item.get("due")
    if source == "messages":
        return item.get("sender")
    if source == "calendar":
        return item.get("start")
    return None


# Sources stored as a bare digest rather than a full id->fp map.
#
# The caller has to carry the baseline in and out of every call, so its size is
# a real cost. The calendar alone is 709 events (~21KB of the 26KB total), and
# for it "something on the calendar changed" plus the current window listing is
# just as actionable as naming the exact event -- unlike grades, where *which*
# assignment got a score is the whole point.
DIGEST_ONLY_DEFAULT = ("calendar", "posts")


def evaluate_source(
    name,
    *,
    items,
    health,
    prev_entry,
    id_key="id",
    meta_key=None,
    include_details=True,
    max_detail_items=25,
    precision="item",
):
    """Diff one source against its previous baseline entry.

    Returns `(payload, baseline_entry, alerts)`. When the source is unhealthy
    the previous entry is returned verbatim so the caller's memory survives a
    bad scrape intact.
    """
    payload = {"status": health.get("status"), "health": health}

    # --- Guard 1: an unhealthy source is never treated as data -------------
    if not health_mod.reportable(health):
        stale_health = dict(health)
        stale_health["stale"] = True
        payload["health"] = stale_health
        payload["changed"] = False
        payload["note"] = (
            "Source unhealthy -- alerts suppressed and the baseline left "
            "unchanged so nothing is missed once it recovers."
        )
        return payload, (prev_entry if isinstance(prev_entry, dict) else None), []

    current = snapshot(items, id_key=id_key)
    prev = prev_entry if isinstance(prev_entry, dict) else None

    # --- digest-only mode: keep the caller's baseline small ----------------
    if precision == "digest":
        prev_value = prev.get("value") if prev else None
        prev_digest = prev_value.get("digest") if isinstance(prev_value, dict) else None
        compact = {"digest": current["digest"], "count": current["count"]}
        entry = {"value": compact}

        payload["count"] = current["count"]
        payload["digest"] = current["digest"]
        payload["precision"] = "digest"

        if prev_digest is None:
            payload["first_run"] = True
            payload["changed"] = False
            payload["note"] = (
                "No baseline supplied, so nothing can be called new. Store the "
                "returned baseline; the next run will report real changes."
            )
            return payload, entry, []

        payload["first_run"] = False
        changed = prev_digest != current["digest"]
        payload["changed"] = changed
        payload["change"] = {
            "previous_digest": prev_digest,
            "previous_count": prev_value.get("count"),
            "delta": current["count"] - (prev_value.get("count") or 0),
        }
        if not changed:
            return payload, entry, []
        payload["note"] = (
            "Tracked as a whole-source digest to keep the baseline small, so "
            "individual items are not identified. The listing accompanying "
            "this response shows the current state."
        )
        return payload, entry, [{
            "kind": f"{name}_changed",
            "source": name,
            "count": current["count"],
            "title": f"{name} changed ({payload['change']['delta']:+d} items)",
            "detail": payload["note"],
        }]

    change = diff(prev.get("value") if prev else None, current)

    entry = {"value": current}

    # A second fingerprint over "cosmetic" fields lets us say *what kind* of
    # change happened: a posted score vs. a teacher fixing a typo.
    meta_change = None
    if meta_key:
        meta_current = snapshot(items, id_key=id_key, fp_key=meta_key)
        entry["meta"] = meta_current
        meta_change = diff(prev.get("meta") if prev else None, meta_current)

    by_id = {}
    for item in items:
        key = str(item.get(id_key))
        by_id.setdefault(key, item)

    payload["count"] = current["count"]
    payload["digest"] = current["digest"]
    payload["first_run"] = change["first_run"]
    payload["change"] = {
        k: v for k, v in change.items() if k != "first_run"
    }
    payload["changed"] = bool(
        change["added"] or change["changed"]
        or (change["removed"] and name in _ALERT_ON_REMOVED)
    )

    # --- Guard 2: first run announces nothing -----------------------------
    if change["first_run"]:
        payload["changed"] = False
        payload["note"] = (
            "No baseline supplied, so nothing can be called new. Store the "
            "returned baseline; the next run will report real changes."
        )
        return payload, entry, []

    prev_total = (prev.get("value") or {}).get("count", 0) if prev else 0
    bulk_threshold = max(_BULK_MINIMUM, int(prev_total * _BULK_FRACTION))

    alerts = []
    details = {}
    for kind in ("added", "changed", "removed"):
        ids = change[kind]
        if not ids:
            continue
        if kind == "removed" and name not in _ALERT_ON_REMOVED:
            continue

        # Bulk turnover: one alert, not hundreds.
        if prev_total and len(ids) >= bulk_threshold:
            alerts.append({
                "kind": f"{name}_bulk_{kind}",
                "source": name,
                "count": len(ids),
                "title": f"{len(ids)} {name} items {kind} at once "
                         f"(previous total {prev_total})",
                "detail": (
                    "A large share of this source turned over in one run. That "
                    "is either a real rollover (new grading period / school "
                    "year) or a partially broken scrape. Worth a look before "
                    "trusting the individual items."
                ),
            })
            details[kind] = [
                _trim(by_id[i], _DETAIL_KEYS.get(name, ()))
                for i in ids[:max_detail_items] if i in by_id
            ]
            details.setdefault("truncated", {})[kind] = max(
                0, len(ids) - max_detail_items
            )
            continue

        shown = []
        for item_id in ids[:max_detail_items]:
            item = by_id.get(item_id)
            if item is None:
                # Only possible for `removed`: it exists in the baseline but
                # not in the current page, so we know nothing about it but the id.
                shown.append({"id": item_id, "known_from_baseline_only": True})
                alerts.append({
                    "kind": _ALERT_KIND.get((name, kind), f"{name}_{kind}"),
                    "source": name, "id": item_id, "title": None,
                })
                continue

            shown.append(_trim(item, _DETAIL_KEYS.get(name, ())))

            alert = {
                "kind": _ALERT_KIND.get((name, kind), f"{name}_{kind}"),
                "source": name,
                "id": item_id,
                "title": _label(name, item),
                "detail": _detail_value(name, item),
                "url": item.get("url") or item.get("assignment_url"),
            }
            if kind == "changed" and meta_change is not None:
                # Reaching here means the substantive hash moved (that is what
                # put the id in `change["changed"]`); the metadata hash may have
                # moved too. A metadata-only edit never enters this loop at all.
                alert["changed_fields"] = (
                    ["grade", "metadata"] if item_id in meta_change["changed"]
                    else ["grade"]
                )
            alerts.append(alert)

        details[kind] = shown
        if len(ids) > max_detail_items:
            details.setdefault("truncated", {})[kind] = len(ids) - max_detail_items

    if include_details and details:
        payload["details"] = details

    return payload, entry, alerts
