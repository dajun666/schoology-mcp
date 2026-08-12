"""Content fingerprints for stateless change detection.

The MCP server keeps NO state about what it has seen before. Instead every
watchable item carries a short content hash, and the *caller* stores those
hashes between runs and hands them back as a `baseline`. Given the same
Schoology data this module always produces the same output, so a caller can
diff two runs reliably without the server remembering anything.

The single most important rule lives with the callers of `fp()`, not here:
**never hash a field that changes on its own.** Schoology renders plenty of
those -- "80 days overdue" (recomputed daily), iCal `DTSTAMP` (regenerated per
request), randomized tooltip DOM ids, JS-filled timestamps that are empty in
the server HTML. Any one of them inside a fingerprint means a false "changed!"
alert every single run. See `parsers.py` for the per-source whitelists.
"""

import hashlib
import json

# Bump whenever any hashed field set changes. A caller whose baseline carries a
# different algo is treated as a first run instead of reporting that every item
# changed -- otherwise editing a parser would page the user about 300 "new"
# grades at 6am.
FP_ALGO = "sgy1"
BASELINE_VERSION = 1

_FP_LEN = 12          # 48 bits; ~1e-9 collision risk at 1000 items
_DIGEST_LEN = 16
_SEP = "\x1f"         # ASCII unit separator -- cannot occur in scraped text


def norm(value) -> str:
    """Normalize a value for hashing: None -> "", collapse all whitespace.

    Whitespace folding matters because BeautifulSoup's `stripped_strings` join
    can vary with insignificant markup reflow.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        # Sorted keys so dict ordering never affects the hash.
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return " ".join(str(value).split())


def fp(*parts) -> str:
    """Short stable hash of the given parts."""
    joined = _SEP.join(norm(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:_FP_LEN]


def snapshot(items, id_key: str = "id", fp_key: str = "fp") -> dict:
    """Build a caller-storable snapshot of a collection.

    Returns `{"digest": str, "count": int, "items": {id: fp}}`. The digest is
    computed over the *sorted* id=fp pairs, so DOM reordering is not a change --
    we care about content, not position.

    Items with a missing or duplicate id get a deterministic `#N` suffix rather
    than silently overwriting each other; losing a row from the map would make
    it invisible to every future diff.
    """
    mapping: dict[str, str] = {}
    for index, item in enumerate(items):
        raw_id = item.get(id_key)
        key = norm(raw_id) or f"_anon{index}"
        if key in mapping:
            suffix = 2
            while f"{key}#{suffix}" in mapping:
                suffix += 1
            key = f"{key}#{suffix}"
        mapping[key] = norm(item.get(fp_key))

    lines = [f"{k}={mapping[k]}" for k in sorted(mapping)]
    digest = hashlib.sha256(_SEP.join(lines).encode("utf-8")).hexdigest()[:_DIGEST_LEN]
    return {"digest": digest, "count": len(mapping), "items": mapping}


def diff(previous: dict | None, current: dict) -> dict:
    """Compare a stored snapshot against a fresh one.

    A missing, malformed or version-mismatched `previous` yields
    `first_run=True` with all three change lists EMPTY. Reporting 300
    pre-existing rows as "added" on the first run would be worse than useless,
    so the caller gets a baseline to store and nothing to announce.
    """
    if not isinstance(previous, dict) or not isinstance(previous.get("items"), dict):
        return {
            "first_run": True,
            "added": [],
            "changed": [],
            "removed": [],
            "unchanged": current.get("count", 0),
        }

    prev_items = previous["items"]
    cur_items = current.get("items", {})

    added = sorted(k for k in cur_items if k not in prev_items)
    removed = sorted(k for k in prev_items if k not in cur_items)
    changed = sorted(
        k for k, v in cur_items.items() if k in prev_items and prev_items[k] != v
    )
    unchanged = len(cur_items) - len(added) - len(changed)

    return {
        "first_run": False,
        "added": added,
        "changed": changed,
        "removed": removed,
        "unchanged": max(0, unchanged),
    }


def coerce_baseline(raw) -> dict:
    """Accept whatever an MCP client managed to send and return a usable dict.

    Clients vary in how they marshal object arguments -- some flatten a dict to
    a JSON string. A baseline we cannot read is treated as absent (first run),
    never as an error: refusing to run because the caller's memory got mangled
    is worse than re-establishing it.
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped or stripped in ("{}", "null", "None"):
            return {}
        try:
            raw = json.loads(stripped)
        except (ValueError, TypeError):
            return {}
    if not isinstance(raw, dict):
        return {}
    if raw.get("v") != BASELINE_VERSION or raw.get("algo") != FP_ALGO:
        # Known-incompatible: keep nothing, force a clean first run.
        return {}
    return raw


def baseline_sources(baseline: dict) -> dict:
    """The per-source snapshot map inside a baseline, tolerantly."""
    sources = baseline.get("sources")
    return sources if isinstance(sources, dict) else {}


def new_baseline(sources: dict, generated_at: str) -> dict:
    return {
        "v": BASELINE_VERSION,
        "algo": FP_ALGO,
        "generated_at": generated_at,
        "sources": sources,
    }
