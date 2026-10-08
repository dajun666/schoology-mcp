# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP server (stdio) for **PAUSD Schoology** (`pausd.schoology.com`), logging in through the
ClassLink SSO portal.

It reads Schoology's **`/v1` REST API** using the browser session's cookie. Schoology documents
that API as OAuth-1.0a-only, but its own front end calls it with the plain session cookie and so do
we — no API key, no signing. The cookie already lives in `storage_state.json`, so **a warm session
answers with no browser process at all**; Playwright is launched only to re-run the ClassLink SAML
flow when the session dies.

A headless-Playwright scraper over the rendered pages is still here as a fallback (`parsers.py`,
`browser.py`), selected by `SCHOOLOGY_API_MODE`. It was the only implementation until 2026-09-01.
Measured on the real account: 3.4–4.8s per page render against 0.12–0.44s per API call, and
`check_updates` across all five sources went 19.3s → 2.2s.

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
test suite. **Run it after touching any parser, transform, fingerprint or health rule.** It needs no
network and no data in the account, and it covers the regressions that matter (determinism,
time-drift, empty-vs-broken, baseline poisoning, first-run silence).

Its `A*` sections cover `apiparse.py` against **committed synthetic fixtures** in
`tests/fixtures/api/`, so that half runs in a fresh clone where the HTML half cannot. Those fixtures
are hand-written and must stay that way: **this repo is public**, so no real section/course id,
student uid, course title or teacher name may ever appear in one — nor in a code comment. Scan
before committing:

```bash
scripts/check_no_real_ids.sh              # what is staged
scripts/check_no_real_ids.sh --worktree   # the whole tree
```

Everything Schoology numbers -- sections, assignments, users, attachments,
grading periods -- is a dense 7+ digit integer, and one is easy to paste in by
accident: a comment explaining a trap reads better with the id that produced it,
and a fixture is quickest to write by copying a real response. Synthetic ids are
therefore written in an **obviously fake shape** -- a leading digit or two
followed by zeros and a short tail (`1000000001`, `2000000011`, `6000000003`) --
and the script treats anything else that long as real. Keep new fixtures to that
shape so the check stays quiet and a reader can tell at a glance.

```bash
python scripts/api_smoke.py             # live /v1 latency check, no browser
python scripts/api_smoke.py --relogin   # allow the browser login fallback
```

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

- `server.py` — FastMCP tool definitions. Owns a module-level `SchoologyClient` (browser) and
  `SchoologyAPI` (/v1), both closed via the `lifespan` contextmanager. Most tools are a pair of
  inner `via_api()` / `via_html()` closures handed to `_via_api()`, which picks one per
  `config.API_MODE` and tags the result with `source`.
- `schoology_mcp/api.py` — `SchoologyAPI`: the /v1 client. httpx, cookies read straight out of
  `storage_state.json`, concurrency cap, token bucket, session-expiry detection. **Imports neither
  Playwright nor `browser.py`** — `relogin_only` is injected as a callable, and that is what keeps
  the warm path browserless. Don't add such an import.
- `schoology_mcp/apiparse.py` — pure `(json) -> dict/list`, the counterpart to `parsers.py`,
  emitting the same output shapes. No I/O, so `check_watch.py --fixtures` exercises all of it.
- `schoology_mcp/browser.py` — `SchoologyClient`: Playwright lifecycle, `fetch()`, keep-alive, plus
  `relogin_only()` for the API path.
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

### The /v1 API path (`api.py`)

Everything below was measured live on 2026-09-01; none of it is documented by Schoology.

**Endpoints that work with a session cookie.** `/v1/users/me` (303s to `/v1/users/<uid>`),
`/v1/users/<uid>/sections`, `/v1/users/<uid>/grades[?section_id=]`,
`/v1/users/<uid>/events?start_date=&end_date=`, `/v1/recent`, `/v1/messages/{inbox,sent}`,
`/v1/sections/<sid>/{assignments,grade_items,grading_categories,updates,documents,events,discussions}`,
`/v1/sections/<sid>/assignments/<aid>`, `/v1/courses/<cid>/folder/<fid>`, `/v1/collections`.

Return 400, don't use: `/v1/users/<uid>/{notifications,messages,courses}`. Returns 403 for a
student: `/v1/sections/<sid>/grades` — use `/v1/users/<uid>/grades` instead. And
**`/v1/users/me/notifications` does not error at all** — it silently serves the `/v1/users/me`
profile, so a 200 is not proof you hit the endpoint you meant.

**The path is `/v1/...`, not `/api/v1/...`.** The latter returns an HTML page with status 200.

**Detecting a dead session took three tries; all three shapes are real:**

| Shape | What it looks like |
|---|---|
| Normal alias | `/v1/users/me` → **303** to `/v1/users/<uid>`. A redirect is NOT the signal. |
| No cookie | **302** to `/login/saml`, followed out to `login.classlink.com`, arriving as **HTML with status 200**. Nothing raises. |
| Stale cookie | **401** on the `/v1` URL itself. |

So liveness is judged by the *final* response being JSON under `/v1/` — the same content-based
reasoning as `_is_logged_in`. **403 is deliberately excluded**: it is a real permission denial, and
re-logging in there would loop.

**Rate limiting.** Successful responses carry no `X-Rate-Limit-*` header, so the budget was
measured: from a rested bucket the **8th request within 2.4s** is refused, while one request every
0.5s ran 20 deep clean. A concurrency cap does not bound a *rate* — twelve calls at concurrency 4
sailed through and the next four were refused — so there is a token bucket (capacity 6, one refill
per 0.5s) as well as `MAX_CONCURRENCY = 4`. The advertised `Retry-After: 10` overstates recovery by
about 8x (measured 1.2s); believing it turned a 15-call sweep into an 11-second one, so it is capped
at 3s and retried.

### Traps in the /v1 data

Each of these is silent — wrong output, no exception.

- **Gradebook section ids are not roster section ids.** `/v1/users/<uid>/grades` reports a
  `section_id` that for half the graded courses matches nothing in `/v1/users/<uid>/sections`. The
  value appears nowhere in the section object except inside the CDN course-logo filename
  (`logo-<gradebook_sid>_…png`), which `_gradebook_to_roster` uses as a fast path with a filtered
  `?section_id=` call as the fallback. Never join on the reported `section_id`.
- **An event id is not an assignment id.** Disjoint sequences — zero overlap across one course's
  twelve events — and `/assignment/<event_id>` is a 403. Build links from the event's own
  `assignment_id`.
- **Titles are HTML-escaped.** Most contain nothing to escape; exactly one row in a 28-item tree
  revealed it. `apiparse._s` unescapes centrally.
- **Event ids are not unique per occurrence.** A teacher creating the same deadline twice yields
  two ids for one item, so dedup keys on `(title, start, section_id)`.
- **The window parameters are `start_date` / `end_date`.** `start` / `end` do not error — they are
  read as paging offsets, and the reply is a confident `{"event": [], "total": 410}`.
- **`/v1/sections/<sid>/assignments` omits hand-made grade columns.** Use `grade_items`, a superset,
  or those rows come back titled `Assignment <id>`.
- **`/v1/users/<uid>/grades` omits a section until something in it is scored.** The page lists that
  course's ungraded rows. Nothing is missed: the first score makes the section appear and its rows
  alert as `added` rather than `changed`.

### What deliberately stays on the HTML path

Speed is not the only axis, and two tools lose more than they gain:

- **`get_messages`** — `/v1/messages/inbox` is 29x faster but returns `message: null` for every row
  and names the sender only by `author_id`. The preview is most of what the tool is for, and the
  only way to get one over the API is to fetch the thread, **which marks it read** — the one side
  effect this server promises not to cause.
- **`get_recent_posts`** — `/v1/recent` even returns the *full* body where the feed markup truncates
  it, but carries no image URLs, and notices here are routinely posted as a picture with no text.

`check_updates` uses the API for both sources anyway: noticing that a thread or post arrived needs
neither a preview nor a picture.

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

### Infinite Campus (`campus.py`, optional)

Gated behind `CAMPUS_ENABLED` (default false) and **conditionally registered** —
when off, `get_schedule` is not added to the tool list at all, so it costs no
schema tokens. The host and ClassLink tile name are district-specific, which is
why it cannot be on by default.

Auth reuses the Schoology flow: `auth.login_app(context, app_name, host_pattern)`
is the generalized tile launcher (`login()` is now a thin wrapper over it).
Infinite Campus is just another ClassLink tile, so cookies for both hosts live in
the same browser context.

The student portal is an SPA — its server HTML is a 19KB shell with no nav in it,
so **do not scrape it**. It is backed by a JSON API (`/campus/resources/portal/
roster`, `/campus/resources/portal/grades`), which is what `campus_json()` calls.
That keeps `campus.py` a pure transform with no selectors to rot.

Two modelling traps the parser exists to flatten:

- The portal emits one placement per **bell schedule**, not per class: `Full`
  plus `M`/`T`/`W`/`R`/`F` and one-off calendar dates. A dozen classes expand
  to fifty-odd rows. `parse_roster` keeps one per term, preferring `Full`, and a
  course lacking that schedule still comes back on whatever placement it has — dropping
  a class from a schedule is worse than showing an odd bell time.
- A year-long course is placed in **both terms**, so it appears twice unless
  `term` is given. A course missing from one semester is usually real data (it
  only runs in the other), not a dropped row — check the placements before
  treating it as a bug.

Period names are not always numeric (counselor slots, advisory, study hall), so
the sort must tolerate non-numbers, and a course can legitimately have no room.

**Never read `/campus/resources/portal/students`** — it returns the student's
legal name, district student number and state ID. The roster carries everything a
schedule needs and none of that.

### Responsive Scheduling (`responsive.py`) — the one tool that writes

Infinite Campus's flex-period picker: a **session** is one flex block on one day,
an **offering** is one teacher's room in it with a seat cap, and the student holds
one offering per session. `get_responsive_schedule` reads it;
`set_responsive_schedule` claims a seat. Two undocumented `prism` endpoints:

```
GET  /campus/resources/prism/portal/responsiveSchedule?calendarID=&personID=&structureID=
POST /campus/resources/prism/portal/responsiveSchedule/update
     {personID, responsiveSessionID, calendarID, responsiveOfferingID} -> {success, message}
```

**This is the server's only write to a school system**, and it is visible to the
school: attendance for that period is taken in the room it picks. Three things
follow.

- **Eligibility is checked before the POST, not after.** The rules in `refusal()`
  are the portal's own, read out of its Angular bundle — an ineligible save
  otherwise costs a round trip and comes back `success: false` carrying a message
  written for someone looking at the page.
- **The three query ids come off the roster**, which every schedule call already
  fetches. The portal's SPA takes them from `/campus/api/portal/students`; this
  server does not read that endpoint (see above), and does not need to.
- **The POST needs Angular's XSRF header** — the `XSRF-TOKEN` cookie echoed back
  as `X-XSRF-TOKEN`, which is what `browser.campus_post_json` adds. Sent only when
  the cookie exists, so a district without the protection still works.

Traps, each silent:

- **`startDate` is midnight in the school's timezone**, as epoch millis. Read as
  UTC it lands on the adjacent day for anyone whose clock is not the school's —
  and the day is the entire content of the field.
- **A held offering at capacity is not "full".** Full means "you cannot take this
  seat"; the seat already held is exempt, or the student's own row reads as one
  they may not stay in. The portal makes the same exception.
- **`teacherRequest` on the *held* offering locks the whole session.** A teacher
  or administrator placed the student, `reason` says why, and no other offering in
  that session may be taken.
- **A session with one offering, already held, is not a choice** — the portal
  disables the form rather than showing a lone radio button that does nothing.
- **A save with no change is rejected**, so re-picking the held seat is refused
  here rather than sent.

Verified live by moving between two offerings and back: the update is an upsert,
so the old seat is released the moment the new one is taken (one seat out, one
seat in, no window where the student holds both) and the reply is
`{"success": true, "message": "This responsive offering was successfully updated."}`.

Sessions with no offerings are dropped, as the portal drops them; `include_empty`
keeps them, because "published but no rooms open yet" is a real state worth
seeing. `check_watch.py`'s `A10` covers all of this against a synthetic fixture.

**Ranking — which teacher to pick — is deliberately not here.** The tools report
seats and eligibility; the choosing belongs to the caller.

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

**`max_bytes` is a real bandwidth guard on the API path.** `downloads.py` is
duck-typed on `get_binary`/`get_binaries`, so it takes either client; `api.py`
streams, refusing an oversized file from its `Content-Length` before any of it
crosses the wire, and abandoning a body that understates its length partway
through. On the legacy `SCHOOLOGY_API_MODE=html` path this is still only a
memory guard — Playwright has no streaming response, so the driver has already
buffered the body before the size is visible.

Two things that path had to get right:

- **Binary requests ask for `Accept-Encoding: identity`.** Schoology answers a
  gzip-encoded request with `Transfer-Encoding: chunked` and no
  `Content-Length`, which defeats the point of streaming — the size cannot be
  checked before the transfer, and an aborted download can only report how much
  arrived, not how big the file is (a 0.5 MB PDF was reported as 0.1 MB, to
  exactly the person deciding whether to raise the limit). With `identity` the
  same PDF declares `Content-Length: 480793`. Nothing is lost: measured on two
  real PDFs, identity was the same speed or slightly faster, since these files
  are already compressed. The mid-stream abort remains as a backstop and marks
  its number `size_is_lower_bound`.
- **Binaries do NOT go through the /v1 token bucket.** The measured rate limit
  applies to the API; these are static files, usually from the asset CDN on a
  different host. Paced through the same bucket, ten course logos took 4.72s
  against 0.14s unpaced. The concurrency cap still applies.

**`https://api.<host>/v1/attachment/…` cannot be fetched by anything.** Its
certificate covers `*.schoology.com`, which does not match a three-label name,
so httpx and Playwright both refuse it — the URL the API hands back is simply
dead. `apiparse.attachment_url` rewrites it onto the main host without the `/v1`
segment, which is the form the materials page links to; the bytes are identical.

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

Two more offenders the API path introduced, both excluded: the grades payload's
`timestamp` (refreshed when a teacher fixes a typo) and a post's `likes` /
`num_comments` (a classmate liking a notice is not a change to the notice).

**`FP_ALGO` is the migration switch.** It is `"sgy2"` since the move to /v1;
`coerce_baseline` treats an unrecognized algo as a clean first run, which is
what stopped the switchover from announcing the entire gradebook as new. Bump it
whenever a hashed field set changes — that is the whole procedure.

**Never fingerprint a window anchored to today.** `check_updates` tracks the
calendar over a fixed school-year window (`apiparse.school_year_window`,
August–July) and filters to the caller's window only for display. A window
anchored to *today* shifts every midnight and its digest with it: one false
"calendar changed" alert per day, forever. This is the same reason the iCal path
hashed the whole feed rather than the requested window.

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
(a period id recurs across courses, and `"0"` is the ungraded bucket), so `parse_grades` emits a course-qualified `uid`. Use that
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
