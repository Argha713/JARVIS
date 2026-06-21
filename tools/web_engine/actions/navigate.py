"""
NavigateAction: opens a browser tab and navigates to a URL.

open_url()   — high-level: extension first, Playwright fallback.
               Returns {"tabId": int} on extension success, or a Playwright tuple on fallback.
open_page()  — low-level Playwright helper used by discoverer, retriever, and login.
               Returns (playwright_ctx, browser, context, page) or None.
close_page() — paired close for open_page() callers.
"""
from loguru import logger
from playwright.sync_api import sync_playwright

from tools.web_engine import store


# ── High-level: extension-first navigation ────────────────────────────────────

def open_url(site_id: str, url: str) -> dict | tuple | None:
    """
    Navigate to url. Tries the browser extension first; falls back to Playwright.
    Returns:
      {"tabId": int, "url": str}  — extension path (caller cannot interact with page object)
      (pw, browser, context, page) — Playwright fallback (caller must call close_page())
      None                         — both paths failed
    """
    from tools.browser_extension import connection_manager
    if connection_manager.is_connected():
        try:
            from tools.browser_extension import run_command_sync
            result = run_command_sync("navigate", {"url": url})
            if result.get("status") == "ok":
                logger.info("[NAV] Extension: navigated to {!r}", url)
                return result.get("data", {})
            logger.warning("[NAV] Extension navigate failed: {}", result.get("error"))
        except Exception as exc:
            logger.warning("[NAV] Extension path error ({}), falling back to Playwright", exc)

    return open_page(site_id, url, headless=True)


# ── Low-level: Playwright page helper (used by discoverer / retriever / login) ─

def open_page(site_id: str, url: str, headless: bool = True,
              timeout_ms: int = 35_000) -> tuple | None:
    """
    Returns (playwright_ctx, browser, context, page) navigated to url, or None on failure.
    Caller MUST call close_page(pw, browser) when done.
    """
    session    = store.load_session(site_id)
    ctx_kwargs = {}
    if session:
        ctx_kwargs["storage_state"] = session
        logger.debug("[NAV] Loading saved session for {}", site_id)
    else:
        logger.warning("[NAV] No session for {} — navigating unauthenticated", site_id)

    logger.info("[NAV] Playwright: navigating to {!r} (headless={})", url, headless)
    try:
        pw      = sync_playwright().start()
        browser = pw.chromium.launch(headless=headless, slow_mo=50)
        context = browser.new_context(**ctx_kwargs)
        page    = context.new_page()
        try:
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            logger.info("[NAV] Playwright: landed on {!r}", page.url)
        except Exception as exc:
            logger.warning("[NAV] goto raised: {}", str(exc)[:120])
        return pw, browser, context, page

    except Exception as exc:
        logger.exception("[NAV] Failed to open {!r}: {}", url, exc)
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
