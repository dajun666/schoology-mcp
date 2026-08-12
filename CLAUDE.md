# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP server (stdio) that scrapes **PAUSD Schoology** (`pausd.schoology.com`) with a headless
Playwright browser, logging in automatically through the ClassLink SSO portal. There is no
Schoology API involved — every tool is "fetch a rendered page, run a BeautifulSoup parser over it".

## Commands

```bash
source .venv/bin/activate            # required in every new shell
pip install -r requirements.txt && playwright install chromium

python scripts/set_credentials.py    # store password in the OS keyring (once)
python scripts/login_check.py --show-browser   # verify login + dump HTML to dumps/
python scripts/explore_materials.py --course <id>  # dump a course's materials pages
python server.py                     # run the MCP server over stdio
npx @modelcontextprotocol/inspector python server.py   # interactive tool testing
```

```bash
python scripts/check_watch.py --fixtures   # offline regression suite over dumps/
python scripts/check_watch.py --live       # same checks against the real account
```

There is no linter or build step, and no unit-test framework — `check_watch.py --fixtures` is the
test suite. **Run it after touching any parser, fingerprint or health rule.** It needs no network
and no data in the account, and it covers the regressions that matter (determinism, time-drift,
empty-vs-broken, baseline poisoning, first-run silence).

Beyond that, verification is empirical: re-run `login_check.py` / `explore_materials.py` to refresh
`dumps/`, then check parser selectors against the fresh HTML. `dumps/` is the de-facto fixture set
for parser work — inspect it before changing a selector rather than guessing at markup.

**`dumps/` must never be committed.** It is git-ignored, and it has to stay that way: those files
are full pages of a real student's gradebook, inbox and activity feed. The pressure to commit them
is real — `check_watch.py --fixtures` is this repo's test suite and a fresh clone cannot run it
without them — but the answer is hand-written synthetic fixtures, never a captured page. The same
goes for `tools/rclone.conf` (OAuth tokens), `storage_state.json` (live session cookies) and the
`gdocs-backup*/` exports.

The server is registered in `.mcp.json` by absolute venv path, so its `schoology__*` MCP tools are
callable in-session — the fastest way to exercise a change end-to-end after restarting the session.

## Architecture

Three layers, strictly separated:

- `server.py` — FastMCP tool definitions. Owns one module-level `SchoologyClient`, closed via the
  `lifespan` contextmanager. Tools normalize user input (URL / `/assignment/NNN` path / bare id →
  fetch path) and pick the `wait_selector` for the page they're loading. `get_material` dispatches
  on URL shape to the right parser.
- `schoology_mcp/browser.py` — `SchoologyClient`: Playwright lifecycle, `fetch()`, keep-alive.
- `schoology_mcp/auth.py` — the ClassLink → Schoology SAML flow. Idempotent: safe to re-call, skips
  credential entry if the ClassLink session is still alive.
- `schoology_mcp/parsers.py` — pure functions `(html, base_url) -> dict/list`. No I/O, no Playwright.
  Keep it that way; it is the only part that is district-agnostic.
- `schoology_mcp/config.py` — all env/`.env` reading and password resolution in one place.

### Session handling (the core non-obvious mechanic)

Schoology sessions expire in minutes. `SchoologyClient.fetch()` handles this so no tool ever has to:

1. Every fetch is serialized under one `asyncio.Lock` — shared by tool calls and the keep-alive task.
2. `_is_logged_in(url, html)` is **content-based**, not URL-based: Schoology serves a logged-out page
   at a normal URL, so the check looks for a password input / `/logout` / site nav. Don't "simplify"
   this to a URL check.
3. On a logged-out page, `fetch()` re-runs `login()` and retries **once**, then persists the refreshed
   `storage_state.json`. A failed second attempt raises rather than returning a logged-out page.
4. A background keep-alive task (`SCHOOLOGY_KEEPALIVE_MINUTES`, default 8) re-visits `/home` to keep
   the sliding session warm so interactive calls rarely pay re-login latency.

**MCP uses stdout for the protocol** — all logging must go to stderr (`server.py` configures this).
Never `print()` to stdout from server or library code.

### Credentials

The password is never in a file. `config.get_password()` resolves: `SCHOOLOGY_PASSWORD` env var →
keyring. Backend selection lives in `config._use_cryptfile()`: native OS keychain by default,
AES-encrypted file (`keyrings.cryptfile`) when `SCHOOLOGY_KEYRING_PASS` is set (headless Linux).
`_get_keyring()` returns `None` rather than prompting or raising — the server path must never block
on an interactive prompt. `scripts/set_credentials.py` deliberately *does* prompt; it reuses
`config._get_keyring()` and only adds the interactive passphrase path.

`SCHOOLOGY_USERNAME` (student ID) stays in `.env` — it is the keyring lookup key, not a secret.

### Parser conventions

- Selectors are written **tolerantly**: cascade through several candidates, fall back to text
  extraction, return `None` for a missing field rather than raising. Schoology markup drifts.
- Each parser section carries a comment documenting the real markup it was verified against. When
  you change a selector, update that comment with what the fresh dump actually showed.
- Known quirks already handled — don't reintroduce them: `/home` renders some assignments twice
  (a hidden "N days overdue" copy plus the normal one), so `parse_upcoming_assignments` dedupes by
  URL; page titles carry a `" | Schoology"` or `"N lesson plans"` suffix that gets stripped.
- Batch operations (`include_info=True`, folder expansion) catch per-item exceptions and attach an
  `info_error` / `fetch_error` field — one bad page must never abort the batch.

### Images in feed posts

`parse_recent_posts` returns an `images` list, and `get_recent_posts` downloads
them. This is not a nicety: notices are often posted purely as a picture, so
`.update-body` yields the empty string and the post reads as blank — five of six
image-bearing posts in `dumps/home.html` have no text at all.

Three `<img>` kinds appear and only one is content: avatars
(`imagecache-profile_*`, on every single post), emoji served by Google Fonts
(replaced in place by their `alt` character *before* the text is extracted, or
they vanish), and the real embed under `/system/files/attachments/page_embeds/`
or `/file_download/`, which needs the session cookie.

Downloads are keyed by **URL basename, not `Content-Disposition`** — the header
name is unknown until after the fetch (so it cannot serve a cache lookup) and
Schoology reuses `image.png` across unrelated posts, so those would overwrite
each other. Files run ~2 MB apiece; the cache is what keeps repeat reads free.

The fingerprint includes the image **count, not the URLs**: an added or removed
picture is a real change, but the embed URLs carry generated suffixes whose
stability across renders is unverified (the live feed had no images to test
against), and a churning URL would alert on every run.

### The download cache (`downloads.py`)

One temp-directory cache shared by Drive exports, Schoology attachments and feed
images, with `fetch_to_cache` / `fetch_many` as the single download path for
anything behind the school login. It is a **cache, not a store** — pruned after
24h, so a returned `path` is to read now, not to keep.

Cache keys come from the **URL basename, never `Content-Disposition`**: the
header name is unknown until after the fetch (so it cannot serve a lookup at
all) and Schoology reuses `image.png` across unrelated posts, which would
overwrite each other. `name_for` parses the URL properly rather than slicing —
naive splitting returns the hostname for a URL with no path.

`get_binaries` takes the browser lock **once** for a batch. These are plain
authenticated GETs with no page, no navigation and no re-login retry, so they
cannot race on the things the lock actually protects; taking it per-call meant
`asyncio.gather` at the call site would simply queue. Six ~2 MB post images went
11.4s → 7.7s (partly bandwidth-bound, so not the full theoretical win).

**`max_bytes` is a memory guard here, not a bandwidth one.** Playwright has no
streaming response, so the driver has already buffered the body before
Content-Length is visible — an oversized Schoology file is declined *after*
transfer. Only the Drive path (rclone `--dry-run` probe) refuses ahead of it.
Don't let the docs claim otherwise.

### Google Docs export (`gdocs.py`)

Teachers often attach a Google Doc instead of uploading a file, so a Schoology
"link" material is frequently the actual content. `get_material` detects
docs.google.com / drive.google.com targets and exports them via
`rclone backend copyid` (project-local `tools/rclone` + `tools/rclone.conf`,
remotes scoped `drive.readonly`, school account `gdrive2:` tried before
`gdrive:`). Native Google files have no download URL — export through the Drive
API is the only way to read them.

This is the server's **only disk write besides `storage_state.json`**, and it is
a cache, not state: exports land in a temp dir (`SCHOOLOGY_EXPORT_DIR`, else
`<tmp>/schoology-mcp-materials/<file_id>/`) and directories older than 24h are
pruned on each call. Change detection still persists nothing.

Three things this module exists to get right:

- **stdout is the MCP protocol channel.** rclone writes to both streams, so the
  subprocess captures both and neither can reach our stdout. Never let it inherit.
- **Google inlines images as base64 data URIs.** One real doc exported to 157,879
  characters; decoding the URIs to image files and substituting their paths cut
  it to 1,695 (1.07%) and left 15 openable PNGs. Reference-style markdown
  (`![][image1]` plus a definition block) is handled because the substitution
  rewrites the *definitions*, keeping numbering aligned.
- **Some docs cannot be exported at all.** If the owner disabled "viewers can
  download, print, copy", the API returns 403 and no amount of retrying helps.
  That surfaces as `google_doc.export_error`, following the existing
  `info_error` / `fetch_error` convention — never a failed call.

`probe()` is the pre-flight check: `rclone backend copyid --dry-run` reports the
filename and size while writing nothing. It exists because `--max-size` is
silently ignored by `copyid`, so there is otherwise no way to decline a large
file before it is already on disk. It also doubles as a type check that beats
URL parsing: **a native Google file reports no size** (it has none until
exported), so size-present means uploaded binary, size-absent means native Doc.
`download_file` gates on it.

`classify()` exists because the URL shape decides everything, and two shapes are
traps. A Form is `/forms/d/e/<id>`, where the obvious `/d/(...)` match yields the
literal `"e"` — a plausible-looking id that sends you off exporting a file that
does not exist; Forms are not files and are rejected up front. And `/file/d/<id>`
is an *uploaded* file, not a native Doc: forcing a markdown export there hands
back a PDF that then gets `read_text()` as if it were Markdown. Uploaded files
are reported but not downloaded unless `allow_binary=True`, because their size is
unknown until the transfer is underway (a real assignment linked a 110 MB .mp4)
and `--max-size` is ignored by `backend copyid`.

`check_updates` does not touch this path, so scheduled runs never shell out.

### Change detection (`fingerprint.py`, `health.py`, `watch.py`)

`check_updates` answers "what changed since last time" **without the server
storing anything**. The caller passes a `baseline` (ids + content hashes) and
gets a fresh one back. `storage_state.json` remains the only runtime disk write;
it is auth state, not data.

Layering: `server.py` fetches and parses → `health.evaluate()` judges whether the
scrape can be trusted → `watch.evaluate_source()` diffs and builds alerts. Both
`health` and `watch` are pure, which is why `scripts/check_watch.py --fixtures`
can exercise the whole thing offline against `dumps/`.

**Never fingerprint a field that changes on its own.** This is the single
easiest way to break the feature, and the symptom is a false alert every day
forever. Confirmed offenders, all currently excluded:

| Field | Why |
|---|---|
| `.event-subtitle` "80 days overdue" | recomputed daily — use `due_iso` |
| `.edge-footer .created` | empty in server HTML, JS-filled |
| `aria-describedby="tooltip-content-…"` | randomized per render |
| iCal `DTSTAMP` / `PRODID` | regenerated per request — never hash raw .ics bytes |
| message `unread` | flips when a human opens the message |

Grade rows carry two hashes: `fp` (grade, comment, submission — alert-worthy)
and `fp_meta` (title, due, weight — noise). That split is what distinguishes
"a score was posted" from "the teacher fixed a typo".

Invariants enforced in `watch.evaluate_source`, each guarding a specific false
alarm — don't remove one without understanding what it prevents:

- an `error`/`suspect` source emits no alerts and echoes the caller's previous
  baseline entry back **verbatim** (a failed scrape must never poison the cursor)
- a first run (no baseline) announces nothing
- `removed` only alerts for grades; elsewhere items leave lists by expiring
- a turnover above ~50% collapses into one aggregate alert

`health.evaluate` matches empty-state markers against **visible text only**.
Schoology ships an i18n bundle containing every UI string on every page, so
matching raw HTML always "finds" the empty-state message — that bug made a
broken scrape look like a confident "nothing here".

Row ids are not globally unique: gradebook period rows reuse ids across courses
(`"1120687"`, `"0"`), so `parse_grades` emits a course-qualified `uid`. Use that
as the snapshot key, never bare `id`.

### Retargeting to another district

Hard-wired to PAUSD via two URLs only: `SCHOOLOGY_BASE_URL` and `CLASSLINK_URL` (defaults in
`config.py`). A non-ClassLink district needs `auth.py` replaced; `parsers.py` should keep working.

## Gotchas

- `page.wait_for_load_state("networkidle")` never settles on Schoology (it polls in the background).
  Both `_load()` and the scripts wrap it in a try/except or use a fixed `wait_for_timeout` instead.
- `SCHOOLOGY_HEADLESS=false` in `.env` to watch the browser while debugging a login or selector.
- `.env`, `storage_state.json`, `credentials.cfg` and `dumps/` are git-ignored and contain
  credentials/session/personal data.
