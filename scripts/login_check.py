"""One-time login check + HTML dumper.

Runs the ClassLink -> Schoology login flow, saves the session to
`storage_state.json`, and dumps the HTML of key pages into `dumps/` so the
upcoming-assignment and recent-post selectors can be verified/refined.

Usage:
    python scripts/login_check.py                 # headless
    python scripts/login_check.py --show-browser  # watch it log in
"""

import asyncio
import logging
import pathlib
import sys

# Make the `schoology_mcp` package importable when run as a script.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from schoology_mcp import config  # noqa: E402
from schoology_mcp.auth import login  # noqa: E402

# Surface the per-step INFO logs from auth.py so it is clear where a login
# stalls (which page, credential step, SSO tile, etc.).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

PAGES = {
    "home": "/home",
    "grades": "/grades/grades",
    "courses": "/courses",
    "calendar": "/calendar",
    "messages": "/messages",
    "messages_sent": "/messages/sent",
    # Optional: drop one of your own assignment URLs in here to also verify
    # parse_assignment_info against real markup.
    #   "assignment": "/assignment/<NNNNNNNN>",
    # NOTE: deliberately no /messages/view/<id> here -- opening a message
    # thread marks it READ on Schoology's side. Dump one by hand only if you
    # accept clearing that message's unread badge.
}


async def main() -> None:
    show = "--show-browser" in sys.argv
    dumps_dir = config.PROJECT_ROOT / "dumps"
    dumps_dir.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not show)
        context = await browser.new_context()

        print("Logging in via ClassLink ...")
        await login(context)
        await context.storage_state(path=str(config.STORAGE_STATE_PATH))
        print(f"OK -- session saved to {config.STORAGE_STATE_PATH}")

        for name, path in PAGES.items():
            url = f"{config.BASE_URL}{path}"
            print(f"dumping {name} <- {url} ...", flush=True)
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                # Schoology polls in the background, so 'networkidle' never
                # settles -- waiting on it just stalls with no output. Give the
                # page a fixed moment to render instead.
                await page.wait_for_timeout(3_000)
                out = dumps_dir / f"{name}.html"
                out.write_text(await page.content(), encoding="utf-8")
                print(f"  saved {out} ({out.stat().st_size:,} bytes)", flush=True)
            except Exception as exc:  # noqa: BLE001 - one bad page must not hang the run
                print(f"  FAILED {name}: {exc}", flush=True)
            finally:
                await page.close()

        await browser.close()

    print("\nDone. Inspect dumps/home.html and dumps/calendar.html to confirm")
    print("the upcoming-assignment and recent-post selectors in parsers.py.")


if __name__ == "__main__":
    asyncio.run(main())
