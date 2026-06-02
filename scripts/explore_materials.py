"""Playwright exploration script for Schoology course materials pages.

Logs in (reusing existing storage_state.json if valid), navigates to the
materials page for the first few courses, and dumps the HTML to dumps/ so
selectors can be confirmed before implementing the parser.

Usage:
    python scripts/explore_materials.py                 # headless
    python scripts/explore_materials.py --show-browser  # watch it run
    python scripts/explore_materials.py --course <id>   # specific course only
"""

import asyncio
import logging
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from schoology_mcp import config  # noqa: E402
from schoology_mcp.auth import login  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

MAX_COURSES = 3  # how many courses to dump (avoid hammering the server)


def _extract_course_ids(html):
    """Pull /course/NNN links from the /courses page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    ids = []
    for a in soup.find_all("a", href=re.compile(r"^/course/\d+$")):
        cid = a["href"].split("/")[-1]
        if cid not in seen:
            seen.add(cid)
            title = " ".join(a.stripped_strings) or f"course-{cid}"
            ids.append((cid, title))
    return ids


def _preview_materials(html):
    """Quick structural preview so we can see selectors without opening the file."""
    soup = BeautifulSoup(html, "html.parser")

    # Try common Schoology materials selectors.
    for selector in (
        ".s-material-row",
        ".material-row",
        ".edge-list-item",
        "li[class*='material']",
        "li[id^='edge-type']",
        "li[class*='edge-type']",
    ):
        rows = soup.select(selector)
        if rows:
            print(f"  selector '{selector}' → {len(rows)} rows")
            for row in rows[:5]:
                cls = " ".join(row.get("class") or [])
                a = row.find("a", href=True)
                title = " ".join(row.stripped_strings)[:60] if row else ""
                print(f"    [{cls[:50]}] {title!r}  href={a['href'] if a else None}")
            if len(rows) > 5:
                print(f"    ... ({len(rows) - 5} more)")
            return

    # Fallback: show all <li> elements that look content-related.
    lis = soup.select("li")
    print(f"  no known selector matched — found {len(lis)} <li> total")
    for li in lis[:8]:
        cls = " ".join(li.get("class") or [])
        if cls:
            print(f"    class={cls[:60]!r}")


async def main() -> None:
    show = "--show-browser" in sys.argv
    specific = None
    if "--course" in sys.argv:
        idx = sys.argv.index("--course")
        if idx + 1 < len(sys.argv):
            specific = sys.argv[idx + 1]

    dumps_dir = config.PROJECT_ROOT / "dumps"
    dumps_dir.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not show)

        if config.STORAGE_STATE_PATH.exists():
            print(f"Reusing session from {config.STORAGE_STATE_PATH}")
            context = await browser.new_context(
                storage_state=str(config.STORAGE_STATE_PATH)
            )
        else:
            context = await browser.new_context()

        # Check if session is still valid; re-login if not.
        page = await context.new_page()
        await page.goto(f"{config.BASE_URL}/home", wait_until="domcontentloaded")
        await page.wait_for_timeout(2_000)
        if "/login" in page.url or 'type="password"' in (await page.content()).lower():
            print("Session expired — logging in via ClassLink ...")
            await page.close()
            await login(context)
            await context.storage_state(path=str(config.STORAGE_STATE_PATH))
            print(f"Session saved to {config.STORAGE_STATE_PATH}")
        else:
            print("Session is valid.")
            await page.close()

        # Enumerate courses.
        if specific:
            course_list = [(specific, f"course-{specific}")]
        else:
            cpage = await context.new_page()
            await cpage.goto(f"{config.BASE_URL}/courses", wait_until="domcontentloaded")
            await cpage.wait_for_timeout(2_000)
            html = await cpage.content()
            await cpage.close()
            course_list = _extract_course_ids(html)[:MAX_COURSES]
            print(f"Found {len(course_list)} courses to dump (capped at {MAX_COURSES})")

        # Dump each course materials page.
        for course_id, course_title in course_list:
            url = f"{config.BASE_URL}/course/{course_id}/materials"
            safe_title = re.sub(r"[^\w\-]", "_", course_title[:30])
            out_path = dumps_dir / f"materials_{course_id}.html"
            print(f"\ndumping '{course_title}' ({course_id}) <- {url} ...")
            mpage = await context.new_page()
            try:
                await mpage.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await mpage.wait_for_timeout(4_000)
                content = await mpage.content()
                out_path.write_text(content, encoding="utf-8")
                print(f"  saved {out_path} ({out_path.stat().st_size:,} bytes)")
                _preview_materials(content)
            except Exception as exc:  # noqa: BLE001
                print(f"  FAILED: {exc}")
            finally:
                await mpage.close()

        await browser.close()

    print("\nDone. Inspect dumps/materials_*.html to confirm selectors.")


if __name__ == "__main__":
    asyncio.run(main())
