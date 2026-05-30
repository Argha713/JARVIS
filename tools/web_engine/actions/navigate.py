"""
NavigateAction: opens a headless (or visible) browser with a saved session
and navigates to a URL. Returns (browser, context, page) for the caller to use.
Caller is responsible for closing the browser.
"""
import json

from loguru import logger
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

from tools.web_engine import store


def open_page(site_id: str, url: str, headless: bool = True,
              timeout_ms: int = 35_000) -> tuple | None:
    """
    Returns (playwright_ctx, browser, page) navigated to url, or None on failure.
    Caller must call browser.close() when done.
    The playwright context manager must be kept alive — caller receives it.
    """
    session = store.load_session(site_id)
    ctx_kwargs = {}
    if session:
        ctx_kwargs["storage_state"] = session
        logger.debug("[NAV] Loading saved session for {}", site_id)
    else:
        logger.warning("[NAV] No session for {} — navigating unauthenticated", site_id)

    logger.info("[NAV] Navigating to {!r} (headless={})", url, headless)
    try:
        pw   = sync_playwright().start()
        browser = pw.chromium.launch(headless=headless, slow_mo=50)
        context = browser.new_context(**ctx_kwargs)
        page    = context.new_page()

        try:
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            logger.info("[NAV] Landed on {!r}", page.url)
        except Exception as e:
            logger.warning("[NAV] goto raised: {}", str(e)[:120])

        return pw, browser, context, page

    except Exception as e:
        logger.exception("[NAV] Failed to open {!r}: {}", url, e)
        return None


def close_page(pw, browser) -> None:
    try:
        browser.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass
