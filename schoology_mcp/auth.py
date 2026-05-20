"""Automated login: ClassLink portal -> Schoology SSO.

PAUSD students do not log into Schoology directly. They sign into the
ClassLink portal (https://launchpad.classlink.com/pausd) with their 8-digit
student ID and password, then click the Schoology tile, which performs a SAML
single sign-on into pausd.schoology.com.

`login()` is safe to call repeatedly for re-login: if the ClassLink session is
still alive it skips the credential step and goes straight to the SSO tile.
"""

import logging
import re

from playwright.async_api import BrowserContext, Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from . import config

log = logging.getLogger("schoology_mcp.auth")

# Selectors are intentionally broad: ClassLink's exact field ids can vary, so
# we try common candidates and fall back to type-based selectors.
_USERNAME_SELECTOR = (
    "input#username, input[name='username'], input[name='user'], "
    "input[autocomplete='username'], input[type='text']:visible"
)
_PASSWORD_SELECTOR = "input#password, input[name='password'], input[type='password']"
_SUBMIT_SELECTOR = (
    "button[type='submit'], input[type='submit'], #signin, #loginBtn, "
    "button:has-text('Sign In'), button:has-text('Log In')"
)


async def login(context: BrowserContext) -> None:
    """Run the ClassLink -> Schoology login flow, populating `context` cookies.

    On success the context holds a valid pausd.schoology.com session. All pages
    opened by this function are closed before returning; the cookies persist in
    the context.
    """
    config.require_credentials()
    portal = await context.new_page()
    schoology: Page | None = None
    try:
        log.info("Opening ClassLink portal: %s", config.CLASSLINK_URL)
        await portal.goto(config.CLASSLINK_URL, wait_until="domcontentloaded")

        username = portal.locator(_USERNAME_SELECTOR).first
        try:
            await username.wait_for(state="visible", timeout=8_000)
        except PlaywrightTimeout:
            # No login form -> the ClassLink session is still active.
            log.info("ClassLink session still active; skipping credential entry")
        else:
            await _submit_credentials(portal, username)

        schoology = await _launch_schoology_tile(context, portal)
        try:
            await schoology.wait_for_url(re.compile(r"schoology\.com"), timeout=45_000)
            await schoology.wait_for_load_state("networkidle", timeout=30_000)
        except PlaywrightTimeout:
            pass

        if "schoology.com" not in schoology.url or "/login" in schoology.url:
            raise RuntimeError(
                f"SSO into Schoology did not complete (landed on {schoology.url})."
            )
        log.info("Logged into Schoology: %s", schoology.url)
    finally:
        for p in (schoology, portal):
            if p is not None and not p.is_closed():
                try:
                    await p.close()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass


async def _submit_credentials(page: Page, username: Locator) -> None:
    """Fill and submit the ClassLink username/password form."""
    await username.fill(config.USERNAME)

    # Password is usually on the same page; some flows reveal it after "Next".
    password = page.locator(_PASSWORD_SELECTOR).first
    try:
        await password.wait_for(state="visible", timeout=3_000)
    except PlaywrightTimeout:
        log.info("Password field not visible yet; submitting username step")
        await page.locator(_SUBMIT_SELECTOR).first.click()
        await password.wait_for(state="visible", timeout=15_000)
    await password.fill(config.get_password())

    log.info("Submitting ClassLink credentials")
    await page.locator(_SUBMIT_SELECTOR).first.click()
    try:
        await page.wait_for_load_state("networkidle", timeout=30_000)
    except PlaywrightTimeout:
        pass

    if await _looks_like_login_error(page):
        raise RuntimeError(
            "ClassLink login failed -- check SCHOOLOGY_USERNAME / SCHOOLOGY_PASSWORD."
        )


async def _launch_schoology_tile(context: BrowserContext, page: Page) -> Page:
    """Click the Schoology app tile on the ClassLink My Apps portal.

    The portal is an Angular app: the Schoology tile is an `<application>`
    custom element with an exact `aria-label="Schoology"` (a pinned `<favorite>`
    with the same label may also exist -- either launches Schoology via SSO).
    Matching `aria-label` exactly avoids clicking a neighbouring app's tile (an
    earlier loose `div:has-text` selector landed on Gale instead).
    The tile usually opens Schoology in a new tab; handle the same-tab case too.
    """
    tile = page.locator(
        "application[aria-label='Schoology'], [aria-label='Schoology']"
    ).first
    await tile.wait_for(state="visible", timeout=30_000)
    await tile.scroll_into_view_if_needed()

    try:
        async with context.expect_page(timeout=15_000) as new_page_info:
            await tile.click()
        return await new_page_info.value
    except PlaywrightTimeout:
        # Opened in the same tab instead of a popup.
        return page


async def _looks_like_login_error(page: Page) -> bool:
    """Heuristic: still on ClassLink with a visible error message.

    Two separate locators: Playwright rejects its `text=` selector engine
    inside a CSS comma-list, so the text match is its own locator.
    """
    if "classlink.com" not in page.url:
        return False
    error_text = re.compile(r"invalid|incorrect|failed|try again", re.I)
    for loc in (
        page.locator(".error, .alert-danger, [role='alert']").first,
        page.get_by_text(error_text).first,
    ):
        try:
            if await loc.is_visible():
                return True
        except Exception:  # noqa: BLE001 - absent element / selector quirk
            continue
    return False
