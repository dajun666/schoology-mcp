"""Export Google Drive documents linked from Schoology materials.

Teachers routinely attach a Google Doc to a course instead of uploading a file,
so `get_material` on such an item used to return nothing but an external URL --
a dead end for a reader. Native Google files have no download URL; they only
come out through the Drive API's export, which is what `rclone backend copyid`
performs.

Credentials and the rclone binary are project-local (`tools/rclone`,
`tools/rclone.conf`, both git-ignored, remotes scoped `drive.readonly`). This
module shells out rather than reusing `scripts/get_gdoc.sh`: that script is
CLI-shaped (progress on stdout, filename recovered by diffing directory
listings), and giving each export its own empty directory makes the result
unambiguous without parsing anything.

Two hazards drive the design:

1. **stdout belongs to the MCP protocol.** rclone writes to both streams, so
   both are captured and neither is ever allowed to reach our stdout.
2. **Google inlines images as base64 data URIs.** An image-heavy doc exports to
   several MB of unreadable text. Those URIs are decoded to real image files
   next to the markdown and replaced with their paths, which keeps the text
   small *and* leaves the pictures somewhere a reader can actually open them.
"""

import asyncio
import base64
import logging
import os
import re
import shutil
from pathlib import Path

from . import config, downloads

log = logging.getLogger("schoology_mcp.gdocs")

RCLONE_BIN = config.PROJECT_ROOT / "tools" / "rclone"
RCLONE_CONF = config.PROJECT_ROOT / "tools" / "rclone.conf"

# Tried in order: the school account first, since Schoology-linked docs live
# there. Merely being shared with an account is enough -- copyid does not care
# about ownership, only read access.
REMOTES = ("gdrive2", "gdrive")

# Drive's quota is roughly 10 transactions/sec; stay under it.
TPS_LIMIT = "8"
EXPORT_TIMEOUT_S = 180
PROBE_TIMEOUT_S = 60

# One ceiling for every download path, defined in `downloads`.
DEFAULT_MAX_BYTES = downloads.DEFAULT_MAX_BYTES

_DRIVE_HOSTS = ("docs.google.com", "drive.google.com")
_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]+)|[?&]id=([A-Za-z0-9_-]+)")
_VALID_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# `data:image/png;base64,AAAA...` as emitted by Google's markdown export.
_DATA_URI_RE = re.compile(r"data:image/([A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)")

# Text returned inline is capped; anything past this stays on disk only.
MAX_INLINE_CHARS = 200_000

_EXTENSION = {"jpeg": "jpg", "svg+xml": "svg", "x-icon": "ico"}


def is_drive_url(url) -> bool:
    return bool(url) and any(host in url for host in _DRIVE_HOSTS)


def classify(url_or_id) -> str:
    """Which kind of Drive target this is. Drives both id parsing and format.

    The distinctions matter:
      - `form`  cannot be exported at all -- a Form is not a file.
      - `file`  is an *uploaded* file (PDF, image, ...) shown in Drive's viewer.
        It is not a native Google doc, so forcing a markdown export would hand
        back a PDF that we would then try to read as text.
    """
    s = (url_or_id or "").lower()
    if "/forms/" in s:
        return "form"
    if "/spreadsheets/" in s:
        return "spreadsheet"
    if "/presentation/" in s:
        return "presentation"
    if "/file/d/" in s:
        return "file"
    # /document/ and bare ids alike: a Doc is the overwhelmingly common case.
    return "document"


def parse_file_id(url_or_id):
    """Extract a Drive file id from any of its URL shapes, or None.

    Note the Forms shape is `/forms/d/e/<id>`, where a naive `/d/(...)` match
    yields the literal "e" -- a plausible-looking id that would send us off to
    export a file that does not exist.
    """
    s = (url_or_id or "").strip()
    if not s:
        return None
    if classify(s) == "form":
        return None
    match = _ID_RE.search(s)
    if match:
        return match.group(1) or match.group(2)
    return s if _VALID_ID_RE.match(s) else None


def export_format(url_or_id):
    """Export format for a target, or None to download the file untouched.

    Docs export to markdown; Sheets and Slides have no markdown form. Uploaded
    files are not native Google formats and must not be export-converted.
    """
    kind = classify(url_or_id)
    if kind == "spreadsheet":
        return "xlsx"
    if kind == "presentation":
        return "pptx"
    if kind in ("file", "form"):
        return None
    return "md"


# The cache itself lives in `downloads.py` -- it holds Schoology attachments and
# feed images too, so it is not Drive-specific. Aliased here because the export
# paths below use it heavily.
export_root = downloads.cache_root
prune_exports = downloads.prune


def available() -> tuple[bool, str | None]:
    """Is the Drive export path usable at all?"""
    if not RCLONE_BIN.exists():
        return False, f"rclone not found at {RCLONE_BIN}"
    if not os.access(RCLONE_BIN, os.X_OK):
        return False, f"rclone at {RCLONE_BIN} is not executable"
    if not RCLONE_CONF.exists():
        return False, f"rclone config not found at {RCLONE_CONF}"
    return True, None


async def _run_rclone(
    remote: str,
    file_id: str,
    dest: Path,
    fmt: str | None,
    extra: tuple[str, ...] = (),
    timeout: int = EXPORT_TIMEOUT_S,
):
    """One `rclone backend copyid` invocation. Returns (ok, combined_output).

    The single place rclone is spawned, so the "never inherit our stdout"
    rule -- stdout is the MCP protocol channel -- is enforced once.
    """
    cmd = [str(RCLONE_BIN), "backend", "copyid", f"{remote}:", *extra]
    if fmt:
        # Only meaningful for native Google formats; omitted for uploaded files
        # so they come down byte-for-byte.
        cmd += ["--drive-export-formats", fmt]
    cmd += [
        "--tpslimit", TPS_LIMIT,
        file_id,
        f"{dest}{os.sep}",   # trailing separator keeps the document's real title
    ]
    env = {**os.environ, "RCLONE_CONFIG": str(RCLONE_CONF)}

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,   # never inherit our stdout: it is the
        stderr=asyncio.subprocess.PIPE,   # MCP protocol channel
        env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"rclone timed out after {timeout}s"
    combined = (out or b"").decode("utf-8", "replace") + (err or b"").decode("utf-8", "replace")
    return proc.returncode == 0, combined.strip()


_SIZE_RE = re.compile(r"\(size\s+([\d.]+)\s*([KMGTP]?i?)B?\)", re.I)
_NAME_RE = re.compile(r"NOTICE:\s+(.+?):\s+Skipped copy as --dry-run is set")
_UNIT_BYTES = {"": 1, "K": 1000, "KI": 1024, "M": 1000**2, "MI": 1024**2,
               "G": 1000**3, "GI": 1024**3, "T": 1000**4, "TI": 1024**4}


def _parse_size(text: str):
    match = _SIZE_RE.search(text)
    if not match:
        return None
    value, unit = match.groups()
    try:
        return int(float(value) * _UNIT_BYTES.get(unit.upper(), 1))
    except (ValueError, TypeError):
        return None


async def probe(file_id: str) -> dict:
    """Ask Drive what a file is and how big, WITHOUT transferring it.

    `rclone backend copyid --dry-run` reports the name it would write and, for
    files that have a fixed size, that size -- while writing nothing. This is
    the only pre-flight check available: `--max-size` is silently ignored by
    `copyid`, so without this there is no way to decline a 110 MB video before
    it is already on disk.

    A *native* Google file (Doc/Sheet/Slides) reports no size, because it has
    none until it is exported. That absence is itself the signal: size present
    means an uploaded binary, size absent means a native Google document.

    Returns {name, size_bytes, native, remote} or {error}.
    """
    ok, why = available()
    if not ok:
        return {"error": why}

    dest = export_root() / f"_probe-{file_id}"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        attempts = []
        for remote in REMOTES:
            ok, text = await _run_rclone(
                remote, file_id, dest, None,
                extra=("--dry-run", "-v"), timeout=PROBE_TIMEOUT_S,
            )
            name_match = _NAME_RE.search(text)
            if ok and name_match:
                return {
                    "name": name_match.group(1).strip(),
                    # Absent for a native Google file: it has no size until it
                    # is exported. That absence is meaningful to the caller.
                    "size_bytes": _parse_size(text),
                }
            attempts.append(f"{remote}: {text.splitlines()[-1] if text else 'failed'}")
        return {"error": " | ".join(attempts)}
    finally:
        shutil.rmtree(dest, ignore_errors=True)


async def export_file(file_id: str, fmt: str | None = "md") -> dict:
    """Export one Drive file. Returns a dict with `path` or `error`.

    Never raises: a document that cannot be exported is reported as a per-item
    error so a batch caller can keep going, matching the info_error /
    fetch_error convention used elsewhere in this codebase.
    """
    ok, why = available()
    if not ok:
        return {"error": why}

    dest = export_root() / file_id
    # A fresh directory per export means the product is simply the only file in
    # it -- no output parsing, and no confusion with a previous download.
    if dest.exists():
        existing = [p for p in dest.iterdir() if p.is_file() and p.suffix != ".part"]
        # Cache hit only when the file we already have matches what was asked
        # for; with no export format any downloaded file will do.
        cached = [p for p in existing if not fmt or p.suffix.lstrip(".") == fmt]
        if cached:
            return {"path": cached[0], "dir": dest, "cached": True}
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    attempts = []
    for remote in REMOTES:
        ok, output = await _run_rclone(remote, file_id, dest, fmt)
        if ok:
            files = [p for p in dest.iterdir() if p.is_file()]
            if files:
                return {"path": files[0], "dir": dest, "remote": remote, "cached": False}
            attempts.append(f"{remote}: reported success but produced no file")
            continue
        attempts.append(f"{remote}: {output.splitlines()[-1] if output else 'failed'}")

    detail = " | ".join(attempts)
    hint = ""
    if "forbidden" in detail.lower() or "403" in detail:
        # The owner disabled "viewers can download, print, copy". No API can
        # export such a file -- it is not a bug and retrying will not help.
        hint = (
            " The owner has disabled download/print/copy for viewers, so no API "
            "can export this document; it has to be read in a browser."
        )
    return {"error": f"Drive export failed. {detail}.{hint}", "dir": dest}


def extract_images(markdown: str, dest: Path) -> tuple[str, list[dict]]:
    """Write inlined base64 images to files and point the markdown at them.

    Google's markdown export embeds every picture as a base64 data URI, so an
    image-heavy document becomes megabytes of text that no reader can use. The
    pictures are worth keeping -- an agent can open an image file -- so they are
    decoded to disk and the URI is replaced by the path.
    """
    images: list[dict] = []

    def replace(match):
        subtype = match.group(1).lower()
        payload = re.sub(r"\s+", "", match.group(2))
        try:
            raw = base64.b64decode(payload, validate=True)
        except (ValueError, TypeError):
            return "<undecodable-image>"
        ext = _EXTENSION.get(subtype, subtype)
        name = f"image-{len(images) + 1:03d}.{ext}"
        path = dest / name
        try:
            path.write_bytes(raw)
        except OSError as exc:
            log.warning("Could not write %s: %s", path, exc)
            return "<unwritable-image>"
        images.append({"name": name, "path": str(path), "bytes": len(raw), "format": ext})
        return str(path)

    return _DATA_URI_RE.sub(replace, markdown), images


async def fetch_document(
    url_or_id: str,
    allow_binary: bool = False,
    max_bytes: int | None = DEFAULT_MAX_BYTES,
) -> dict:
    """Export a Drive document and return readable content plus image paths.

    Uploaded files (`/file/d/<id>` — PDFs, images, video) are NOT downloaded
    unless `allow_binary` is set. A Drive link in a Schoology assignment can be
    anything: the first one encountered in testing was a 110 MB .mp4, which is
    both useless to a reader and a rude thing to pull silently. rclone's
    `--max-size` is ignored by `backend copyid`, so there is no way to cap this
    mid-transfer — the only guard is refusing to start, which is what
    `max_bytes` does here (`None` disables it).

    The cap lives at this level, not at a call site, so every caller gets it:
    `get_material(allow_binary=True)` would otherwise reach the exporter with no
    limit at all.
    """
    kind = classify(url_or_id)
    if kind == "form":
        # A Form is a live web app, not a file; there is nothing to export.
        return {
            "kind": "form",
            "url": url_or_id,
            "export_error": "Google Forms cannot be exported -- open the link in a browser.",
        }

    file_id = parse_file_id(url_or_id)
    if not file_id:
        return {"export_error": f"Could not parse a Drive file id from {url_or_id!r}"}

    fmt = export_format(url_or_id)

    # Only uploaded files have a size before transfer (a native Google file has
    # none until it is exported), so the probe is worth its ~1-3s only for them.
    if kind == "file":
        meta = await probe(file_id)
        if not allow_binary:
            payload = {
                "kind": "file",
                "file_id": file_id,
                "url": url_or_id,
                "skipped": "binary",
                "note": (
                    "This is an uploaded file (PDF, image, video...), not a "
                    "Google Doc, so it was not downloaded. Use `download_file` "
                    "on this URL if you want it."
                ),
            }
            if "error" not in meta:
                payload["filename"] = meta["name"]
                payload["size_bytes"] = meta["size_bytes"]
            return payload

        if "error" in meta:
            return {"file_id": file_id, "kind": kind, "format": fmt,
                    "export_error": f"Drive lookup failed: {meta['error']}"}

        size = meta.get("size_bytes")
        if max_bytes is not None and size is not None and size > max_bytes:
            refusal = downloads.too_large(size, max_bytes)
            return {
                "file_id": file_id, "kind": kind, "format": fmt,
                "filename": meta["name"],
                **refusal,
                "export_error": f"{meta['name']} is {refusal.pop('error')}",
            }

    prune_exports()
    result = await export_file(file_id, fmt)
    if "error" in result:
        return {"file_id": file_id, "kind": kind, "format": fmt,
                "export_error": result["error"]}

    path: Path = result["path"]
    payload = {
        "file_id": file_id,
        "kind": kind,
        "format": fmt,
        "filename": path.name,
        "path": str(path),
        "export_dir": str(result["dir"]),
        "bytes": path.stat().st_size,
        "cached": result.get("cached", False),
    }

    if fmt != "md":
        # Sheets/Slides export to binary Office formats, and an uploaded file
        # (kind == "file") comes down as whatever it already was -- a PDF, an
        # image. Reading either as text would produce garbage, so hand back the
        # path and let the caller open it.
        payload["note"] = (
            "Downloaded as-is; this is an uploaded file, not a Google Doc. "
            "Open it at `path`."
            if fmt is None else
            f"Exported as .{fmt} (Google offers no markdown form for this file "
            "type). Open the file at `path`."
        )
        return payload

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        payload["export_error"] = f"Exported but unreadable: {exc}"
        return payload

    text, images = extract_images(text, result["dir"])
    payload["images"] = images
    payload["image_count"] = len(images)
    if len(text) > MAX_INLINE_CHARS:
        payload["content"] = text[:MAX_INLINE_CHARS]
        payload["truncated"] = True
        payload["note"] = (
            f"Content truncated at {MAX_INLINE_CHARS:,} characters; the whole "
            "document is at `path`."
        )
    else:
        payload["content"] = text
        payload["truncated"] = False
    return payload
