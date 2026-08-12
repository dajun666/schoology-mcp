# Schoology MCP

An MCP server for **PAUSD Schoology** (`https://pausd.schoology.com`). It drives
a headless browser that **logs in automatically** through the ClassLink portal
and exposes tools to fetch grades, upcoming assignments and recent posts.

It builds on the scraping approach from `dajun666/schoology-get` (Playwright +
BeautifulSoup), and adds the missing piece: **automated login** — no more
hand-exporting `cookies.json`.

## How login works

PAUSD students reach Schoology through ClassLink. The server:

1. Opens `https://login.classlink.com/my/pausd` and fills the student ID +
   password.
2. Clicks the **Schoology** tile, which performs SAML SSO into Schoology.
3. Saves the session to `storage_state.json` so later runs skip the login until
   it expires (then it logs in again automatically).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # do this in every new shell

pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# edit .env: set SCHOOLOGY_USERNAME (your 8-digit student ID)

# store the password in the OS keychain -- not in any file:
python scripts/set_credentials.py
```

The `python …` commands below assume this `.venv` is activated. The MCP server
itself runs outside any shell, so it is registered with the venv's Python by
absolute path — see *Register with Claude Code*.

## Credentials & password storage

The password is **never stored in a plaintext file**. `set_credentials.py`
saves it to a keyring (encrypted at rest) via the `keyring` library, and the
server reads it from there at runtime. The backend is chosen automatically per
platform:

| Platform | Backend | Setup |
|----------|---------|-------|
| macOS | Keychain | works out of the box |
| Windows desktop | Credential Manager | works out of the box |
| Linux desktop | Secret Service (GNOME Keyring / KWallet) | install `gnome-keyring` / `libsecret` and run a keyring daemon |
| Headless Linux (server, WSL, Docker, cron) | AES-encrypted file (`keyrings.cryptfile`) | set a master passphrase — see below |

- Update it later: re-run `python scripts/set_credentials.py`.
- Remove it: `python scripts/set_credentials.py --delete`.
- macOS/Windows may ask once to allow access to the stored item — choose **Always
  Allow** so the unattended server isn't blocked.
- `SCHOOLOGY_USERNAME` (the student ID) stays in `.env`; it is the keyring
  lookup key, not a secret.
- Fallback: if you set the `SCHOOLOGY_PASSWORD` environment variable, it is used
  instead of the keyring (handy for throwaway/CI use).

### Headless Linux (no OS keychain)

There is no OS secret store on a headless box, so use the encrypted-file
backend. A master passphrase both selects it and unlocks it:

```bash
export SCHOOLOGY_KEYRING_PASS='your-master-passphrase'
python scripts/set_credentials.py        # stores to ~/.local/share/schoology-mcp/credentials.cfg (AES)
```

Then run the **server** with the **same** `SCHOOLOGY_KEYRING_PASS` in its
environment — e.g. a systemd `EnvironmentFile` (perms `600`) or the MCP client's
`env` block. Without the passphrase the server cannot decrypt the file and will
fail with a clear error rather than falling back to plaintext.

Caveat: a keyring protects the password at rest and keeps it out of dotfiles,
backups and git — but any process running as your user can still read it (and
for the encrypted file, anyone who has both the file and the passphrase). It is
strictly better than a plaintext `.env`, not a sandbox.

## Verify login (do this first)

```bash
python scripts/login_check.py --show-browser
```

This logs in, saves `storage_state.json`, and dumps page HTML into `dumps/`.
All four parsers are verified against a real PAUSD account (grades, courses,
upcoming assignments, recent posts). If Schoology changes its markup later,
re-run this and re-check the selectors in `schoology_mcp/parsers.py` against
the fresh `dumps/`.

## Tools

| Tool | Description |
|------|-------------|
| `get_grades` | Current grades for every course (periods, categories, assignments). |
| `get_courses` | Enrolled courses. |
| `get_upcoming_assignments` | Upcoming / due-soon assignments and events. Pass `include_info=True` to also fetch each item's description (slower). |
| `get_assignment_info` | Full details (title, course, due, description, attachments) of one assignment. Takes a URL, `/assignment/NNN` path, or bare id. |
| `get_recent_posts` | Latest posts from the activity feed, with embedded images downloaded. |
| `get_course_materials` | All materials in a course (folders, assignments, docs, pages, links). |
| `get_material` | Open one material by URL; dispatches on type. Google Docs links are exported to Markdown (see below). |
| `get_messages` | Inbox / sent list: subject, sender, date, body preview. Does not open anything. |
| `get_message_thread` | Full text of one thread. **Marks it read** — see below. |
| `get_calendar_events` | School calendar (holidays, no-school days, deadlines) via the iCal feed. |
| `check_updates` | **What changed since last time.** See *Reminders* below. |
| `download_file` | Download any Drive or Schoology link; size-checked before transferring. |
| `get_health` | Is the scraper working, or is the account just empty? |

## Images in feed posts

School notices are routinely posted as a picture with no words at all. Six of
the ten posts in a real feed sample carried an image, and **five of those six
had no text whatsoever** — to a text-only reader they were blank.

`get_recent_posts` therefore returns an `images` list per post and downloads
each one by default, so every picture comes back with a `path` you can open:

```json
{"author": "Student Activities Office", "text": "",
 "images": [{"url": "https://pausd.schoology.com/system/files/.../notice.gif",
             "path": "/tmp/schoology-mcp-materials/schoology/notice_6a05f62e.gif",
             "bytes": 2250950, "content_type": "image/gif"}]}
```

Three kinds of `<img>` appear in a post and only one is content: profile
avatars are ignored, emoji (served as images by Google Fonts) are folded back
into the text as characters, and the actual embedded picture is fetched with
the session cookie.

Images are cached by URL, so re-reading the feed costs nothing — the sample
above is ~11 MB the first time and 0 ms after. Pass `download_images=False` for
URLs only. `check_updates` never downloads them; scheduled runs stay light.

## Google Docs materials

Teachers frequently attach a Google Doc rather than uploading a file, so a
Schoology "link" material is often the actual content. `get_material` exports
those to Markdown and returns them under `google_doc`:

```json
{"type": "link", "title": "...", "url": "https://docs.google.com/document/d/...",
 "google_doc": {"content": "# ...", "images": [...], "image_count": 15,
                "path": "/tmp/schoology-mcp-materials/<id>/<title>.md"}}
```

Embedded images are written out as real image files and the Markdown points at
them, so a reader can actually open them. This matters more than it sounds:
Google inlines every picture as a base64 data URI, and on a real document that
was 157,879 characters of unreadable text — 1,695 after extraction, with 15
usable PNGs alongside.

Requires the project-local rclone setup (`tools/rclone`, `tools/rclone.conf`,
Drive remotes scoped read-only). Exports go to a temp directory
(`SCHOOLOGY_EXPORT_DIR`, default `<tmp>/schoology-mcp-materials/`), cached per
document and pruned after 24h. Pass `export_google_docs=False` to skip it.

### Downloading anything else

Teachers also paste Drive links *inside* assignment descriptions, where they are
not attachments and nothing auto-exports them. Rather than have the server
chase every link it finds, `download_file` lets the agent decide:

```
download_file("https://drive.google.com/file/d/<id>/view")
→ {"filename": "Ch7-Photosynthesis-Lab-Demo.mp4", "size_mb": 118.4,
   "declined": "too_large",
   "error": "118.4 MB, over the 25 MB limit. Not downloaded -- raise max_mb to fetch it."}
```

**For Drive, size is checked before any transfer.** `rclone backend copyid
--dry-run` reports the name and size while writing nothing, which is the only
pre-flight check available — `--max-size` is silently ignored by `copyid`. A
native Google Doc reports no size at all (it has none until exported), and that
absence is how the two are told apart.

For Schoology-hosted attachments there is no equivalent: Playwright has no
streaming response, so the body is already buffered by the time Content-Length
can be read. There `max_mb` keeps an oversized file out of the cache rather than
off the wire.

Accepts Google Drive links and Schoology-hosted attachments (fetched with the
logged-in session). Not a general web downloader.

What gets exported, by link shape:

| Link | Result |
|---|---|
| `/document/d/<id>` | Markdown + extracted images |
| `/spreadsheets/d/<id>` | `.xlsx`, path returned |
| `/presentation/d/<id>` | `.pptx`, path returned |
| `/file/d/<id>` (uploaded PDF/image/video) | **reported, not downloaded** |
| `/forms/d/e/<id>` | not exportable — a Form is not a file |

Uploaded files are skipped by default because their size is unknown until the
transfer is already happening — the first one encountered in a real assignment
was a 110 MB video — and rclone's `--max-size` is ignored by `backend copyid`,
so there is no way to abort mid-flight. Pass `allow_binary=True` when you
actually want one.

Documents whose owner disabled "viewers can download, print, copy" cannot be
exported by any API; those report `google_doc.export_error` instead of failing
the call. For bulk export, use `scripts/backup_gdocs.sh` /
`scripts/get_gdoc.sh` instead.

## Read-only

Every tool issues reads. There is no send-message, post or submit endpoint, and
none is planned for now.

One caveat worth stating plainly: **`get_message_thread` marks that message read
on Schoology's side**, exactly as if the student had clicked it. It is the only
server-visible side effect in the whole MCP. `check_updates` never calls it, so
scheduled checks never touch anyone's unread badges. Use `get_messages` to
browse; use `get_message_thread` only when you actually need the full text.

## Reminders (grades, assignments, messages)

`check_updates` answers *"what changed since last time?"* — the basis for an
agent that tells you when a grade gets posted.

**The server is stateless.** It stores nothing about previous runs. You pass in
the `baseline` from the last call, and it hands you a fresh one:

```python
result = check_updates(baseline=<your stored baseline>)
# -> {"alerts": [...], "changed": ["grades"], "baseline": {...}, ...}
```

The baseline is ~5KB of ids and content hashes. Grades, assignments and messages
are tracked per item, so an alert can name the assignment that got a score. The
calendar is tracked as one whole-feed digest — 709 events would otherwise
dominate the baseline. Pass `detailed_sources=["calendar"]` if you need
per-event precision.

### Keeping the LLM out of the quiet path (recommended)

Most checks find nothing — grades post in bursts, not continuously. Waking an
agent to discover that costs ~3k tokens every time, and the agent adds nothing
on a run with no news.

`scripts/watch_once.py` does the polling without a model. It prints nothing and
exits `0` when there is no change, and only on a real change does it emit a
payload and exit `10`:

```bash
python scripts/watch_once.py --json > /tmp/sgy.json
[ $? -eq 10 ] && <wake your agent with the contents of /tmp/sgy.json>
```

| | Quiet run | Run with news |
|---|---|---|
| Agent woken | no | yes |
| Tokens | **0** | one turn, alerts already in hand |
| Wall clock | ~25s (4 sources) / ~12s (`--sources messages upcoming`) | same |

Because quiet runs cost no tokens, you can poll far more often than you could
afford to wake an agent — the only real budget is page loads against Schoology.
The emitted payload already contains the alerts, so the woken agent does not
need to call `check_updates` again to find out what happened.

It is not a reimplementation: it calls the same `check_updates` and plays the
caller role described below, just holding the baseline in a file
(`~/.local/share/schoology-mcp/watch-state.json`, override with `--state` or
`SCHOOLOGY_WATCH_STATE`) instead of in a conversation. **The MCP server stays
stateless** — that file belongs to the caller, which here is the script.

Exit codes: `0` quiet, `10` changes on stdout, `1` error (state left untouched,
nothing reported). Use `--dry-run` to report without committing state.

Use the agent-driven flow below instead when you want the agent to *judge* what
matters ("is this worth interrupting me for?") on every check rather than only
after the script has already found something.

### Wiring it to a schedule

Any scheduler works (cron, launchd, a Claude Code scheduled task). The agent it
invokes should do exactly this:

1. Read your stored baseline, call `check_updates(baseline=...)`.
2. Notify from `alerts`.
3. **Then** store the returned `baseline`.

Storing *after* notifying is deliberate: a crash in between costs you a
duplicate notification, never a missed one. Because change detection is content
hashing rather than a timestamp cursor, replaying the same baseline twice
produces an identical result, and a missed run just makes the next diff bigger.
Nothing can slip between two runs.

Suggested layout for the agent's own memory:

| File | Contents | Written |
|---|---|---|
| `cursor.json` | the `baseline` object, verbatim | every run |
| `failures` | consecutive-failure counter | on error |

#### Registering with an external agent (OpenClaw shown)

```bash
openclaw mcp add schoology \
  --command /path/to/schoology-mcp/.venv/bin/python \
  --arg /path/to/schoology-mcp/server.py \
  --env SCHOOLOGY_USERNAME=950XXXXX \
  --env SCHOOLOGY_KEEPALIVE=false \
  --include 'check_updates,get_health,get_messages,get_grades,get_calendar_events,get_material,download_file'
```

The tool whitelist is doing two jobs. It keeps `get_message_thread` out of reach
of an unattended agent — that tool marks mail read, the one server-visible side
effect here — and it holds down cost, since **every listed tool's schema is
re-read on every single run**: 7 tools is ~2,110 tokens against ~3,170 for all 13.

Note that `get_material` normally takes a URL discovered via
`get_course_materials`. Alerts already carry `url`, so the whitelist above is
enough to follow up on something that changed; add `get_course_materials`
(+~230 tokens/run) only if the agent should browse a course on its own.

Also note `get_material` can shell out to rclone and write to the export cache.
That is fine on demand, but it means a scheduled agent that decides to open a
material will spawn a subprocess unattended. `check_updates` never does.

### What it will and will not wake you for

- Reports: a score posted or changed, a new assignment, a new message, a
  calendar change. A teacher fixing a typo in an assignment title is classified
  as `metadata` and raises no alert.
- Stays silent: the first run (no baseline means nothing can be "new"); any
  source that failed to scrape; assignments or events simply expiring off a list.
- If a lot changes at once (new grading period, or a partly broken scrape) you
  get **one** aggregate alert instead of hundreds.

### Failure handling for the calling agent

- `status: "error"` → do not touch your stored baseline. Stay quiet for the
  first couple of failures; if it persists, report it and call `get_health()`.
- `status: "suspect"` on a source means *"parsed 0 items but your baseline had
  many"* — treated as a broken scrape, not as vanished grades. Alerts are
  suppressed and that source's baseline is echoed back unchanged.
- Sources you did not request are passed through untouched, so a cheap
  `sources=["messages"]` run will not damage your grades baseline.

### Token cost

A quiet run (nothing changed) costs roughly **3.7k tokens**, measured:

| Component | Tokens |
|---|---|
| Tool schemas, 7-tool whitelist | ~2,110 |
| Baseline passed in | ~1,235 |
| Response | ~340 |
| Model output | ~15 |

Two things keep it there. When nothing moved the reply says `baseline_unchanged`
instead of resending an identical 1.2k-token baseline (so the model never has to
echo it back either), and per-source health collapses to a one-word status
unless you pass `verbose=True` or something is actually wrong.

**Polling frequency is by far the biggest lever** — it is pure multiplication:

| Schedule | Per day | Per month |
|---|---|---|
| every 30 min | ~177k | ~5.3M |
| hourly | ~88k | ~2.7M |
| every 2 hours | ~44k | ~1.3M |
| hourly, school hours only (`0 8-18 * * 1-5`) | ~37k | ~1.1M |

Grades post in bursts, not continuously, so hourly during school hours catches
essentially everything for a fifth of the cost of 30-minute polling. Restrict the
tool whitelist too — every unused tool's schema is re-read on every single run.

All of which is moot if you use `watch_once.py` above: a quiet run there costs
nothing at all, and these numbers apply only when there is actually something to
report.

Cheaper still: split the schedule. `sources=["messages","upcoming"]` skips both
the gradebook page and the calendar feed, so run that often and the full check
a few times a day.

### Scheduling notes

- Set `SCHOOLOGY_KEEPALIVE=false` for cron-style runs. Each run is a fresh
  ~30s process, so the 8-minute keep-alive never fires and only competes for the
  browser lock.
- Schoology sessions are short, so an hourly job usually pays a full ClassLink
  login (tens of seconds). If you want frequent polling, run the server
  long-lived instead so the session stays warm.
- Don't poll more often than about every 30 minutes. Grades post in bursts,
  each check is several headless page loads, and this is an SSO'd student
  account.

### Verifying it

```bash
python scripts/check_watch.py --fixtures   # offline, no login, no network
python scripts/check_watch.py --live       # against the real account
```

The fixture suite is the one to run after touching a parser: it covers the
false-alarm regressions (time-drift, empty-vs-broken, baseline poisoning,
first-run silence). Run it before trusting any scheduled job.

## Run

```bash
python server.py            # speaks MCP over stdio
```

Test interactively with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector python server.py
```

## Register with Claude Code

```bash
claude mcp add schoology -- /your/path/to/schoology-mcp/.venv/bin/python /your/path/to/schoology-mcp/server.py
```

Or add it to an MCP client config (e.g. Claude Desktop `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "schoology": {
      "command": "/your/path/to/schoology-mcp/.venv/bin/python",
      "args": ["/your/path/to/schoology-mcp/server.py"],
      "env": {
        "SCHOOLOGY_USERNAME": "950XXXXX"
      }
    }
  }
}
```

No password appears in the config — it is read from the OS keychain (set once
via `scripts/set_credentials.py`). `SCHOOLOGY_USERNAME` can come from `.env` or
the `env` block above.

## Sessions & auto-refresh

Schoology sessions expire quickly. The server handles this automatically:

- **Detect-and-retry** — every tool call checks (by page content, not just URL)
  whether the page it got back is really logged in. If the session died, it
  re-logs in via ClassLink and retries — a call never silently returns
  logged-out data.
- **Keep-alive** — a background task re-visits Schoology every
  `SCHOOLOGY_KEEPALIVE_MINUTES` (default 8) to keep the session warm, so
  interactive calls rarely wait for a fresh login.
- **Persistence** — the refreshed session is saved to `storage_state.json`, so
  restarting the server reuses it instead of logging in again.

Set `SCHOOLOGY_KEEPALIVE=false` to disable the background task; detect-and-retry
still applies.

## Notes

- `.env` and `storage_state.json` hold credentials/session — they are
  git-ignored. Never commit them.
- Set `SCHOOLOGY_HEADLESS=false` in `.env` to watch the browser while debugging.

## Contributing

Feature requests and issues are always welcomed — open one on the
[issue tracker](https://github.com/dajun666/schoology-mcp/issues) or send a PR.

## Forking for your own district

This repo is hard-wired to PAUSD (`pausd.schoology.com` + the ClassLink
`/pausd` tenant). It is intentionally easy to retarget: fork the repo and
edit the two URLs (and, if your district doesn't use ClassLink SSO,
`schoology_mcp/auth.py`).

| What to change | Where |
|---|---|
| Schoology base URL | `SCHOOLOGY_BASE_URL` in `.env` (or the default in `schoology_mcp/config.py`) |
| ClassLink tenant URL | `CLASSLINK_URL` in `.env` (e.g. `https://login.classlink.com/my/<your-district>`) |
| Login flow (if not ClassLink) | `schoology_mcp/auth.py` — replace the ClassLink portal step with your district's IdP (Clever, Google SSO, direct Schoology login, etc.). The Schoology-side scraping in `schoology_mcp/parsers.py` is district-agnostic and should keep working. |

If you ship a working fork for another district, open an issue with a link —
we can list known-good forks here.

## License

[MIT](LICENSE) — fork it, ship it, no warranty.

## Star History

<a href="https://www.star-history.com/?repos=dajun666%2Fschoology-mcp&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=dajun666/schoology-mcp&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=dajun666/schoology-mcp&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=dajun666/schoology-mcp&type=date&legend=top-left" />
 </picture>
</a>
