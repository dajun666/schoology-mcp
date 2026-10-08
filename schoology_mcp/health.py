"""Distinguish "legitimately empty" from "the scraper broke".

A parser that returns `[]` is ambiguous: it means both "this student has no
grades yet" (true every August) and "Schoology renamed a CSS class". Reporting
the second as the first produces the worst possible alert -- *"all your grades
disappeared"* -- and, if it were allowed to advance the caller's baseline, the
real grades would later come back looking like brand new items.

So every source is judged on two independent signals:

1. **`_is_logged_in()`** (in `browser.py`) already rejects logged-out pages, but
   it is necessary and NOT sufficient: it passes on any authenticated page,
   including a Schoology error page rendering full site chrome.
2. **Skeleton anchors** -- container selectors that are present whether or not
   the page has any items. If not one of them matched, the markup drifted and
   the item count is meaningless.

The `suspect` state is the important one: anchors are fine and the page parsed
cleanly, but a source that had plenty of items last time now has none. That is
reported as a health problem, never as data, and the caller's baseline for that
source is left untouched so nothing is missed once the page recovers.
"""

from bs4 import BeautifulSoup

# How many previously-seen items make a sudden drop to zero suspicious. Below
# this a disappearance is plausibly real (a single assignment expiring off the
# upcoming list); above it, silence is far more likely to be a broken selector.
SUSPECT_THRESHOLD = 5

OK = "ok"
EMPTY = "empty"
SUSPECT = "suspect"
ERROR = "error"

# Statuses whose parsed items may be shown to the user / turned into alerts.
REPORTABLE = (OK, EMPTY)

# Tags whose text is markup, not content. Schoology ships a large JS i18n
# bundle on every page containing *every* UI string it might ever render --
# including the empty-state messages we look for. Matching an empty marker
# against the raw HTML therefore always succeeds (e.g. the key
# "core.override_roles.no_grades_edit_materials" contains "No Grades"), which
# would silently downgrade a broken scrape to a confident "there's nothing
# here". Markers are matched against visible text only.
_NON_CONTENT_TAGS = frozenset({"script", "style", "noscript", "template", "title"})


def visible_text(element) -> str:
    """Text a human would see, excluding script/style/i18n payloads."""
    if element is None:
        return ""
    parts = [
        s for s in element.strings
        if s.parent is not None and s.parent.name not in _NON_CONTENT_TAGS
    ]
    return " ".join(" ".join(parts).split())


def _blank(item_count: int, prev_count: int | None, error: str | None,
           elapsed_ms: int | None) -> dict:
    return {
        "status": ERROR,
        "anchor_found": False,
        "item_count": item_count,
        "empty_confirmed": False,
        "prev_count": prev_count,
        "warning": None,
        "error": error,
        "elapsed_ms": elapsed_ms,
        "stale": False,
    }


def _classify_counts(health: dict, item_count: int, prev_count: int | None) -> dict:
    """The count-based half of the judgement, shared by HTML and feed sources.

    Kept separate so a non-HTML source (the iCal feed has no page skeleton to
    anchor on) reaches the identical empty/suspect policy instead of a hand-
    rolled copy that silently misses the next change to it.
    """
    if item_count > 0:
        health["status"] = OK
    elif prev_count is not None and prev_count >= SUSPECT_THRESHOLD:
        health["status"] = SUSPECT
        health["warning"] = "count_collapsed"
        health["error"] = (
            f"parsed 0 items but the baseline had {prev_count}. Treating this as "
            "a scraping failure rather than data -- alerts suppressed and the "
            "baseline left unchanged."
        )
    else:
        health["status"] = EMPTY
    return health


def evaluate_feed(
    item_count: int,
    prev_count: int | None = None,
    error: str | None = None,
    extra: dict | None = None,
) -> dict:
    """Judge a non-HTML source (the iCal feed) with the same policy as a page.

    There is no page skeleton to anchor on; a 200 response carrying the
    expected body marker is the equivalent guarantee, which the fetch already
    enforced -- so `anchor_found` is True by construction.
    """
    health = _blank(item_count, prev_count, error, None)
    if extra:
        health.update(extra)
    if error:
        return health
    health["anchor_found"] = True
    return _classify_counts(health, item_count, prev_count)


def evaluate(
    html: str | None,
    *,
    anchors: tuple[str, ...],
    empty_markers: tuple[str, ...] = (),
    item_count: int,
    prev_count: int | None = None,
    error: str | None = None,
    elapsed_ms: int | None = None,
) -> dict:
    """Classify one scraped source as ok / empty / suspect / error."""
    health = _blank(item_count, prev_count, error, elapsed_ms)

    if error or html is None:
        health["error"] = error or "no html returned"
        return health

    soup = BeautifulSoup(html, "html.parser")

    matched = [sel for sel in anchors if soup.select_one(sel) is not None]
    health["anchor_found"] = bool(matched)
    health["anchors_matched"] = matched

    if not matched:
        health["warning"] = "markup_drift"
        health["error"] = (
            f"none of the page skeleton selectors matched: {list(anchors)}. "
            "The page layout probably changed -- re-run scripts/login_check.py "
            "and re-check the selectors before trusting any counts."
        )
        return health

    if item_count == 0:
        # Zero items, but the page skeleton is intact. Is that real? An explicit
        # empty-state message settles it; otherwise fall through to the shared
        # count policy.
        content = soup.select_one("#main-inner") or soup.select_one("#center-inner") or soup
        lowered = visible_text(content).lower()
        marker = next((m for m in empty_markers if m.lower() in lowered), None)
        if marker:
            health["status"] = EMPTY
            health["empty_confirmed"] = True
            health["empty_marker"] = marker
            return health

    return _classify_counts(health, item_count, prev_count)


def reportable(health: dict) -> bool:
    """May this source's items be surfaced as data / turned into alerts?"""
    return health.get("status") in REPORTABLE
