import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from .config import Config

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler(sys.stderr))

# Unattended budget. Generous, because a working silent SSO still has to walk
# the whole SAML redirect chain. A dead session is caught by MS_STUCK_MS below
# rather than by burning this whole budget.
SILENT_SSO_TIMEOUT_MS = 90_000
# Sitting on the Microsoft sign-in host this long means the profile's session is
# gone and a human has to re-seed it. No point waiting for the full budget.
MS_STUCK_MS = 10_000
# Interactive bootstrap: the user types the password and approves MFA by hand.
INTERACTIVE_TIMEOUT_MS = 600_000
_POLL_INTERVAL_S = 0.5

_BOOTSTRAP_HINT = (
    "No Brightspace SSO session in the browser profile. Run this once from a "
    "terminal with a real desktop session (NOT under xvfb-run):\n"
    f"  uv run --directory {Path(__file__).resolve().parents[2]} brightspace-mcp-login"
)


class LoginRequiredError(RuntimeError):
    """The saved browser profile has no usable SSO session."""


class ProfileBusyError(RuntimeError):
    """Another process already holds the persistent browser profile."""


async def try_restore_session(config: Config) -> list[dict] | None:
    """Try to restore cookies from saved storage state."""
    if not config.storage_state_path.exists():
        return None

    try:
        data = json.loads(config.storage_state_path.read_text())
        cookies = data.get("cookies", [])
        if not cookies:
            return None
        return cookies
    except (json.JSONDecodeError, KeyError):
        return None


def landed_on_home(url: str, base_url: str) -> bool:
    """True when url is the signed-in Brightspace home page.

    Compares the parsed hostname and path, never a substring of the whole URL.
    The AAD SAML request carries the Brightspace URL inside its RelayState
    query parameter, so a substring test matches while the browser is still
    sitting on the Microsoft sign-in page. Uses .hostname rather than .netloc
    so that case differences and an explicit :443 do not break the match.
    """
    parts = urlparse(url)
    if parts.hostname != urlparse(base_url).hostname:
        return False
    return parts.path == "/d2l/home" or parts.path.startswith("/d2l/home/")


async def _launch(p: Playwright, config: Config, *, headless: bool) -> BrowserContext:
    """Open the persistent Chromium profile that holds the Microsoft SSO session."""
    try:
        return await p.chromium.launch_persistent_context(
            str(config.profile_dir),
            headless=headless,
            args=["--disable-gpu", "--disable-blink-features=AutomationControlled"],
        )
    except PlaywrightError as e:
        # Chromium's ProcessSingleton refuses a profile another process already
        # holds. That process exits before writing, so the session stays intact.
        if "ProcessSingleton" in str(e) or "already in use" in str(e):
            raise ProfileBusyError(
                f"The browser profile at {config.profile_dir} is already open in "
                "another process. That could be the MCP server, a sync script, or "
                "a stray Chromium. Only one process may use it at a time. Close "
                "the other one and retry."
            ) from e
        raise


def _page_of(context: BrowserContext) -> Page | None:
    return context.pages[0] if context.pages else None


async def _await_home(
    page: Page, config: Config, timeout: int, *, unattended: bool
) -> None:
    """Wait until the page lands on Brightspace home, else raise.

    Polls page.url rather than calling wait_for_url(). The SAML hand-off aborts
    navigations, and wait_for_url() surfaces any aborted navigation as
    "net::ERR_ABORTED; maybe frame was detached?" in the middle of a login that
    is actually going fine.
    """
    deadline = time.monotonic() + timeout / 1000
    ms_stuck_deadline = time.monotonic() + MS_STUCK_MS / 1000

    while True:
        if page.is_closed():
            raise RuntimeError("Browser window was closed before login completed")
        if landed_on_home(page.url, config.brightspace_url):
            return
        # A human mid-bootstrap is *expected* to sit on the Microsoft page, so
        # only the unattended path reads that as a dead session. Passed in
        # explicitly: inferring it from the timeout value silently inverts if
        # anyone retunes the constants.
        if (
            unattended
            and time.monotonic() >= ms_stuck_deadline
            and urlparse(page.url).hostname == Config.MS_LOGIN_HOST
        ):
            raise LoginRequiredError(_BOOTSTRAP_HINT)
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(_POLL_INTERVAL_S)

    if urlparse(page.url).hostname == Config.MS_LOGIN_HOST:
        raise LoginRequiredError(_BOOTSTRAP_HINT)
    raise RuntimeError(f"Login never reached Brightspace home. Stuck at {page.url}")


async def _save(context: BrowserContext, config: Config) -> list[dict]:
    await context.storage_state(path=str(config.storage_state_path))
    return await context.cookies()


async def sso_login(config: Config) -> list[dict]:
    """Refresh the Brightspace session from the saved browser profile.

    Unattended. The persistent profile carries the Microsoft refresh token, so
    navigating to Brightspace re-authenticates silently, with no credentials
    and no MFA. Raises LoginRequiredError if the profile has no session left,
    or ProfileBusyError if another process is holding the profile.

    Returns the list of cookies from the authenticated session.
    """
    async with async_playwright() as p:
        context = await _launch(p, config, headless=config.headless)
        try:
            page = _page_of(context) or await context.new_page()
            logger.info("Navigating to Brightspace — profile should carry the session")
            await page.goto(
                f"{config.brightspace_url}/d2l/login", wait_until="domcontentloaded"
            )
            await _await_home(page, config, SILENT_SSO_TIMEOUT_MS, unattended=True)
            logger.info("Silent SSO succeeded — saving session")
            return await _save(context, config)
        finally:
            await context.close()


async def interactive_login(config: Config) -> list[dict]:
    """One-time manual login that seeds the persistent browser profile.

    Opens a real, visible Chromium window. The user types the password and
    approves MFA. Answer "Yes" to "Stay signed in?" — that is what lets
    sso_login() run unattended afterwards.
    """
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError(
            "No display available. Run this from a desktop terminal, not over a "
            "bare SSH session."
        )

    async with async_playwright() as p:
        context = await _launch(p, config, headless=False)
        try:
            page = _page_of(context) or await context.new_page()
            await page.goto(
                f"{config.brightspace_url}/d2l/login", wait_until="domcontentloaded"
            )

            # domcontentloaded fires before AAD renders its form, so wait for
            # the field rather than probing for it once.
            if config.username:
                try:
                    await page.wait_for_selector(Config.MS_EMAIL_INPUT, timeout=15_000)
                    await page.fill(Config.MS_EMAIL_INPUT, config.username)
                except PlaywrightTimeoutError:
                    pass  # Already signed in, or a different sign-in screen

            print(
                "Complete the login in the browser window.\n"
                "  - enter your password\n"
                "  - approve MFA\n"
                '  - answer "Yes" to "Stay signed in?"\n'
                "Waiting up to 10 minutes...",
                file=sys.stderr,
            )
            await _await_home(page, config, INTERACTIVE_TIMEOUT_MS, unattended=False)
            print("Login successful — session saved.", file=sys.stderr)
            return await _save(context, config)
        finally:
            await context.close()


def login_cli() -> None:
    """Console entry point: seed the browser profile with a manual SSO login."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        config = Config()
        asyncio.run(interactive_login(config))
    except KeyboardInterrupt:
        print("Aborted before login completed.", file=sys.stderr)
        raise SystemExit(130) from None
    except (RuntimeError, OSError, PlaywrightError) as e:
        print(f"Login failed: {e}", file=sys.stderr)
        raise SystemExit(1) from e
    print(f"Profile: {config.profile_dir}", file=sys.stderr)
    print(f"Cookies: {config.storage_state_path}", file=sys.stderr)
