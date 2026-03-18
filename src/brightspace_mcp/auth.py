import json
import logging
import sys

from playwright.async_api import async_playwright

from .config import Config

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler(sys.stderr))


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


async def sso_login(config: Config) -> list[dict]:
    """Authenticate to Brightspace via Langara Office365 SSO using Playwright.

    Opens a Chromium browser, navigates to Brightspace login, fills in
    Microsoft credentials, waits for MFA if needed, and saves the session.

    Returns the list of cookies from the authenticated session.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=config.headless,
            args=["--disable-gpu", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # Navigate to Brightspace login — triggers SAML redirect to Microsoft
            logger.info("Navigating to Brightspace login...")
            await page.goto(f"{config.brightspace_url}/d2l/login", wait_until="networkidle")

            # Wait for Microsoft login page
            await page.wait_for_selector(Config.MS_EMAIL_INPUT, timeout=30_000)
            logger.info("Microsoft login page loaded")

            # Fill email
            await page.fill(Config.MS_EMAIL_INPUT, config.username)
            await page.click(Config.MS_SUBMIT_BUTTON)

            # Wait for password field
            await page.wait_for_selector(Config.MS_PASSWORD_INPUT, timeout=15_000)
            await page.fill(Config.MS_PASSWORD_INPUT, config.password)
            await page.click(Config.MS_SUBMIT_BUTTON)

            # Wait for either:
            # - MFA prompt (user must approve manually)
            # - "Stay signed in?" prompt
            # - Direct redirect to Brightspace /d2l/home
            logger.info("Waiting for MFA / redirect (up to 120s)...")
            await page.wait_for_url(
                lambda url: "/d2l/home" in url or "kmsi" in url.lower(),
                timeout=120_000,
            )

            # Handle "Stay signed in?" prompt if present
            current_url = page.url
            if "kmsi" in current_url.lower() or await page.query_selector(Config.MS_SUBMIT_BUTTON):
                try:
                    submit = await page.query_selector(Config.MS_SUBMIT_BUTTON)
                    if submit:
                        await submit.click()
                        await page.wait_for_url(lambda url: "/d2l/home" in url, timeout=15_000)
                except Exception:
                    pass  # May already have redirected

            # Verify we landed on Brightspace
            if "/d2l/home" not in page.url:
                await page.wait_for_url(lambda url: "/d2l/home" in url, timeout=30_000)

            logger.info("Login successful — saving session")

            # Save full storage state (cookies + localStorage)
            await context.storage_state(path=str(config.storage_state_path))

            cookies = await context.cookies()
            return cookies

        finally:
            await browser.close()
