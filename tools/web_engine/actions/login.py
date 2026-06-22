"""
LoginAction: ensures a valid session exists for a site.
- Loads saved session from SQLite.
- If expired/missing: opens a visible browser, waits for user to log in,
  saves fresh session back to SQLite.
"""
import time
from pathlib import Path

from loguru import logger
from playwright.sync_api import sync_playwright

from tools.web_engine import store

LOGIN_TIMEOUT_MS = 120_000   # 2 min for user to log in


def ensure_session(site_id: str, narration) -> bool:
    """
    Ensures a fresh session exists for the site.
    When the browser extension is connected the user's real browser is already
    authenticated — no Playwright login needed. Returns True immediately.
    Falls back to the Playwright login flow when the extension is unavailable.
    Returns True if session is ready, False if login timed out.
    """
    logger.info("[LOGIN] ensure_session: site={!r}", site_id)

    from tools.browser_extension import connection_manager
    ext_connected = connection_manager.is_connected()
    logger.info("[LOGIN] extension connected: {}", ext_connected)

    if ext_connected:
        logger.info("[LOGIN] Extension path — user's real browser is authenticated, no Playwright login needed")
        return True

    logger.info("[LOGIN] Extension not connected — checking saved Playwright session")
    session = store.load_session(site_id)
    has_session = session is not None
    logger.info("[LOGIN] saved session found: {}", has_session)

    if session:
        logger.debug("[LOGIN] Session for {} is valid", site_id)
        return True

    site = store.get_site(site_id)
    if not site:
        logger.error("[LOGIN] Unknown site: {}", site_id)
        return False

    base_url = site["base_url"]
    logger.info("[LOGIN] No valid session for {} — opening browser for login", site_id)
    narration.say(
        "The browser is open, sir. Please log in and I'll wait right here."
    )

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, slow_mo=100)
            context = browser.new_context()
            page    = context.new_page()

            try:
                page.goto(base_url, timeout=30_000, wait_until="domcontentloaded")
            except Exception:
                pass

            page.wait_for_timeout(2_000)
            logger.info("[LOGIN] URL after settle: {!r}", page.url)

            # Wait until the user lands on the site domain (logged in)
            try:
                page.wait_for_url(
                    lambda url: (
                        _domain(site_id) in url and
                        "sign" not in url.lower() and
                        "login" not in url.lower() and
                        "auth" not in url.lower()
                    ),
                    timeout=LOGIN_TIMEOUT_MS,
                )
                page.wait_for_timeout(2_000)
                logger.info("[LOGIN] Login complete | URL: {!r}", page.url)
            except Exception:
                browser.close()
                logger.error("[LOGIN] Login timed out for {}", site_id)
                return False

            # Save session
            session_data = context.storage_state()
            store.save_session(site_id, session_data)
            browser.close()

        narration.say("You're logged in, sir. Give me a moment.")
        return True

    except Exception as e:
        logger.exception("[LOGIN] Unexpected error: {}", e)
        return False


def _domain(site_id: str) -> str:
    """Extract domain from site_id (which is the domain)."""
    return site_id.split("/")[0]
