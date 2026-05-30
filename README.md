# Schoology MCP

An MCP server for **PAUSD Schoology** (`https://pausd.schoology.com`). It drives
a headless browser that **logs in automatically** through the ClassLink portal
and exposes tools to fetch grades, upcoming assignments and recent posts.

It builds on the scraping approach from `dajun666/schoology-get` (Playwright +
BeautifulSoup), and adds the missing piece: **automated login** — no more
hand-exporting `cookies.json`.

## How login works

PAUSD students reach Schoology through ClassLink. The server:

1. Opens `https://launchpad.classlink.com/pausd` and fills the student ID +
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
| `get_recent_posts` | Latest posts from the activity feed. |

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
| ClassLink tenant URL | `CLASSLINK_URL` in `.env` (e.g. `https://launchpad.classlink.com/<your-district>`) |
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
