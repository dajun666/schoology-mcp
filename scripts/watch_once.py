"""Poll Schoology once and say nothing unless something actually changed.

This exists to keep an LLM out of the quiet path. A scheduled agent that wakes
up, loads tool schemas, passes a baseline in and reasons about "nothing changed"
costs ~3k tokens *per run*, and the overwhelming majority of runs are quiet --
grades post in bursts, not continuously.

So: this script does the polling instead. It is plain Python (no model, no
tokens), and it prints nothing and exits 0 when there is no news. Only when
something really changed does it emit a payload, which is the signal to wake
the agent -- and that payload already contains the alerts, so the agent does
not have to call `check_updates` again just to find out what happened.

It is not a second implementation of anything: it calls the same MCP tool the
agent would, and plays the caller role the README documents -- it just holds the
baseline in a file instead of in a conversation. The MCP server itself remains
stateless; this file belongs to the caller.

Exit codes (designed for shell chaining):
    0   no change -- stay quiet
    10  changes found -- payload on stdout
    1   error -- state left untouched, nothing reported

Usage:
    python scripts/watch_once.py                     # human-readable
    python scripts/watch_once.py --json              # machine-readable
    python scripts/watch_once.py --sources messages upcoming
    python scripts/watch_once.py --dry-run           # never persist state

Wiring it to an agent (only fires when there is news):

    python scripts/watch_once.py --json > /tmp/sgy.json
    [ $? -eq 10 ] && openclaw agent run --message "$(cat /tmp/sgy.json)"
"""

import argparse
import asyncio
import json
import logging
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from schoology_mcp import config  # noqa: E402

DEFAULT_STATE = config.WATCH_STATE_PATH

EXIT_QUIET = 0
EXIT_CHANGED = 10
EXIT_ERROR = 1


def load_state(path: pathlib.Path) -> dict:
    """Return the stored state, or {}. A damaged file is not fatal.

    A corrupt state file degrades to a first run, which is silent by design --
    strictly better than crashing a scheduled job, and self-healing on the run
    after that.
    """
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (ValueError, OSError) as exc:
        print(f"warning: ignoring unreadable state at {path}: {exc}", file=sys.stderr)
        return {}


def save_state(path: pathlib.Path, state: dict) -> None:
    """Persist atomically -- a torn write would poison the next run's diff."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def render(result):
    """Human-readable summary of what changed."""
    lines = [f"Schoology: {len(result['alerts'])} update(s) at {result['generated_at']}"]
    for alert in result["alerts"]:
        title = alert.get("title") or alert.get("id")
        detail = alert.get("detail")
        line = f"  [{alert['kind']}] {title}"
        if detail and detail != title:
            line += f" -- {detail}"
        if alert.get("url"):
            line += f"\n      {alert['url']}"
        lines.append(line)
    return "\n".join(lines)


async def run(args) -> int:
    import server  # heavy import: constructs the browser client

    state_path = pathlib.Path(args.state)
    state = load_state(state_path)

    try:
        result = await server.check_updates(
            baseline=state.get("baseline"),
            sources=args.sources or None,
            max_detail_items=args.max_items,
            # Every cron run is a fresh process, so the server's in-memory feed
            # URL cache is always cold. Handing back the URL we stored last time
            # avoids re-deriving it -- two headless page loads, the bulk of a
            # quiet run's wall clock.
            ical_url=state.get("ical_url"),
        )
    except Exception as exc:  # noqa: BLE001 - a scheduled job must not traceback
        print(f"error: Schoology check failed: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        await server.client.close()

    if result["status"] == "error":
        # Every source failed. Say so on stderr and leave the baseline alone so
        # the next successful run still diffs against the last known-good state.
        problems = {
            name: payload.get("health")
            for name, payload in result["sources"].items()
        }
        print(f"error: all sources failed: {json.dumps(problems, default=str)}",
              file=sys.stderr)
        return EXIT_ERROR

    alerts = result["alerts"]

    # Emit BEFORE persisting. If this process dies in between, the next run
    # reports the same thing again -- a duplicate notification, never a lost one.
    if alerts:
        if args.json:
            json.dump(result, sys.stdout, default=str)
        else:
            print(render(result))
        sys.stdout.flush()

    if not args.dry_run:
        # `baseline` is absent when nothing moved; keep what we already have.
        updated = dict(state)
        if result.get("baseline") is not None:
            updated["baseline"] = result["baseline"]
        if result.get("ical_url"):
            updated["ical_url"] = result["ical_url"]
        updated["saved_at"] = result["generated_at"]
        if updated != state:
            save_state(state_path, updated)

    if not alerts and args.verbose:
        print(f"no changes ({result['status']})", file=sys.stderr)

    return EXIT_CHANGED if alerts else EXIT_QUIET


def main():
    parser = argparse.ArgumentParser(
        description="Poll Schoology; print only when something changed.",
    )
    parser.add_argument("--state", default=str(DEFAULT_STATE),
                        help=f"baseline file (default: {DEFAULT_STATE})")
    parser.add_argument("--sources", nargs="*", default=None,
                        help="grades upcoming messages calendar posts")
    parser.add_argument("--json", action="store_true", help="emit JSON, not text")
    parser.add_argument("--max-items", type=int, default=25)
    parser.add_argument("--dry-run", action="store_true",
                        help="report but never write the state file")
    parser.add_argument("--verbose", action="store_true",
                        help="log quiet runs to stderr")
    args = parser.parse_args()

    # The MCP server logs to stderr at INFO; a cron job wants silence.
    logging.basicConfig(level=logging.WARNING if args.verbose else logging.ERROR)

    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
