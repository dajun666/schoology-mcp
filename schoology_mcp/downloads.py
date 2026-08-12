"""The on-disk cache for anything this server fetches, and one way to fill it.

Three different things end up here -- Google Drive exports, Schoology
attachments, and images embedded in feed posts -- so the cache does not belong
to any one of them. It previously lived in `gdocs.py`, which meant a module
named for Google Docs owned a directory named "schoology" and `server.py` had
to import the rclone module in order to save a JPEG.

It is a **cache, not a store**: everything lands in a temp directory and is
pruned after a day. Treat a returned `path` as something to read now, not
somewhere to keep things.

`fetch_to_cache` is the single download path for URLs behind the Schoology
login. Post images and attachments were two hand-written copies of it that had
already drifted -- only one stripped the query string when deriving a filename
(so the other could never hit the cache), and only one consulted the cache at
all.
"""

import logging
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from . import config

log = logging.getLogger("schoology_mcp.downloads")

# Files run to a few MB (a post image is typically a ~2 MB full-resolution
# flyer), so this is a real ceiling, not a formality.
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
MAX_AGE_HOURS = 24.0

_SUBDIR = "files"


def cache_root() -> Path:
    """Where downloads land. A temp location -- this is a cache, not a store."""
    root = (
        Path(config.EXPORT_DIR) if config.EXPORT_DIR
        else Path(tempfile.gettempdir()) / "schoology-mcp-materials"
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def prune(max_age_hours: float = MAX_AGE_HOURS) -> None:
    """Delete cached downloads older than `max_age_hours`. Best effort.

    Both directories (one per Drive file id) and loose files are aged out. The
    files matter: attachments share a single directory whose mtime is refreshed
    by every new write, so a directory-only sweep would never expire anything
    inside it and the temp area would grow without bound.

    Failures are ignored -- a cache that cannot be tidied is not a reason to
    fail a read.
    """
    cutoff = time.time() - max_age_hours * 3600
    try:
        entries = list(cache_root().rglob("*"))
    except OSError:
        return
    for child in entries:
        try:
            if child.stat().st_mtime >= cutoff:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except OSError:
            continue


def name_for(url: str) -> str:
    """Cache key for a URL: the basename of its path, without query or fragment.

    Derived from the URL rather than `Content-Disposition` for two reasons: it
    is known *before* the fetch, so a cache lookup is possible at all; and it is
    unique per file, whereas Schoology serves `image.png` as the disposition
    name for unrelated posts, which would overwrite each other.

    Parsed properly rather than sliced: naive splitting on a URL with no path
    yields the hostname as the filename.
    """
    path = urlparse(url or "").path
    return Path(path.rstrip("/")).name or "download"


def path_for(name: str) -> Path:
    """Where `save` would put this name."""
    return cache_root() / _SUBDIR / Path(name).name  # never escape the cache dir


def cached(name: str) -> Path | None:
    """An already-downloaded, non-empty copy of `name`, if there is one."""
    path = path_for(name)
    try:
        if path.is_file() and path.stat().st_size > 0:
            return path
    except OSError:
        pass
    return None


def save(name: str, body: bytes) -> Path:
    """Write a downloaded file into the cache and return its path."""
    prune()
    path = path_for(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def too_large(size_bytes: int, max_bytes: int) -> dict:
    """The one rendering of "this file is over the limit".

    Written once so the wording and the rounding cannot drift between the three
    paths that can decline a download.
    """
    return {
        "declined": "too_large",
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / 1024 / 1024, 1),
        "error": (
            f"{size_bytes / 1024 / 1024:.1f} MB, over the "
            f"{max_bytes / 1024 / 1024:.0f} MB limit. Not downloaded -- "
            "raise max_mb to fetch it."
        ),
    }


def _from_response(name: str, got: dict, max_bytes: int) -> dict:
    """Turn one `get_binary` result into a cache entry or an error dict."""
    if got.get("too_large"):
        return too_large(got["size_bytes"], max_bytes)
    if "error" in got:
        return {"error": got["error"]}
    return {
        "path": str(save(name, got["body"])),
        # The disposition name is worth reporting even though it is not the
        # cache key -- it is usually the nicer human-readable one.
        "filename": got.get("filename") or name,
        "bytes": got["size_bytes"],
        "content_type": got.get("content_type"),
        "cached": False,
    }


def _hit(name: str) -> dict | None:
    path = cached(name)
    if path is None:
        return None
    return {
        "path": str(path), "filename": name,
        "bytes": path.stat().st_size, "cached": True,
    }


async def fetch_many(client, urls: list[str], max_bytes: int = DEFAULT_MAX_BYTES) -> list[dict]:
    """Fetch several URLs into the cache, concurrently, preserving order.

    Cache hits are resolved first so only the misses go over the network -- the
    same flyers reappear on every feed read, and re-fetching ~2 MB apiece is
    pure waste.
    """
    names = [name_for(u) for u in urls]
    results: list[dict | None] = [_hit(n) for n in names]

    misses = [i for i, r in enumerate(results) if r is None]
    if misses:
        prune()  # once for the batch, not once per file
        fetched = await client.get_binaries([urls[i] for i in misses], max_bytes)
        for i, got in zip(misses, fetched):
            results[i] = _from_response(names[i], got, max_bytes)
    return [r for r in results if r is not None]


async def fetch_to_cache(client, url: str, max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """Download a URL behind the Schoology login into the cache.

    Returns `{path, filename, bytes, content_type, cached}` on success, or a
    dict carrying `error` (and `declined`/`size_bytes` when it was refused for
    size). Never raises: callers attach the error to the item they were fetching
    and keep going, matching the info_error / fetch_error convention.
    """
    name = name_for(url)
    hit = _hit(name)
    if hit is not None:
        return hit

    try:
        got = await client.get_binary(url, max_bytes=max_bytes)
    except Exception as exc:  # noqa: BLE001 - one bad file must not abort a batch
        return {"error": str(exc)}
    return _from_response(name, got, max_bytes)
