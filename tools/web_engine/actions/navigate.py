"""
NavigateAction: opens a browser tab and navigates to a URL.

open_url()   — high-level: extension first, Playwright fallback.
open_page()  — low-level Playwright helper used by discoverer, retriever, and login.
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
      {"tabId": int, "url": str}  — extension path
      (pw, browser, context, page) — Playwright fallback (caller must call close_page())
      None                         — both paths failed
    """
    logger.info("[NAV.open_url] site={!r} url={!r}", site_id, url)

    from tools.browser_extension import connection_manager
    ext_connected = connection_manager.is_connected()
    logger.info("[NAV.open_url] extension connected: {}", ext_connected)

    if ext_connected:
        logger.info("[NAV.open_url] PATH → Extension (primary)")
        try:
            from tools.browser_extension import run_command_sync
            logger.debug("[NAV.open_url] Sending navigate command …")
            result = run_command_sync("navigate", {"url": url})
            status = result.get("status")
            data   = result.get("data", {})
            logger.info("[NAV.open_url] Extension navigate result: status={!r} data={}", status, data)

            if status == "ok":
                logger.info("[NAV.open_url] Extension PATH success — tabId={}", data.get("tabId"))
                return data
            logger.warning("[NAV.open_url] Extension navigate status={!r} error={!r} — falling back", status, result.get("error"))
        except Exception as exc:
            logger.warning("[NAV.open_url] Extension PATH error: {} {} — falling back to Playwright", type(exc).__name__, exc)
    else:
        logger.info("[NAV.open_url] Extension not connected → going straight to Playwright")

    # Playwright fallback
    logger.info("[NAV.open_url] PATH → Playwright (fallback)")
    return open_page(site_id, url, headless=True)


# ── Low-level: Playwright page helper ────────────────────────────────────────

def open_page(site_id: str, url: str, headless: bool = True,
              timeout_ms: int = 35_000) -> tuple | None:
    """
    Returns (playwright_ctx, browser, context, page) navigated to url, or None on failure.
    Caller MUST call close_page(pw, browser) when done.
    """
    logger.debug("[NAV.open_page] site={!r} url={!r} headless={}", site_id, url, headless)

    session    = store.load_session(site_id)
    has_session = session is not None
    logger.info("[NAV.open_page] saved session found: {}", has_session)

    ctx_kwargs = {}
    if session:
        ctx_kwargs["storage_state"] = session
        logger.debug("[NAV.open_page] Loading saved session for {}", site_id)
    else:
        logger.warning("[NAV.open_page] No session for {} — navigating unauthenticated", site_id)

    logger.info("[NAV.open_page] Launching Playwright Chromium headless={} …", headless)
    try:
        pw      = sync_playwright().start()
        browser = pw.chromium.launch(headless=headless, slow_mo=50)
        context = browser.new_context(**ctx_kwargs)
        page    = context.new_page()

        logger.debug("[NAV.open_page] Navigating to {!r} (timeout={}ms) …", url, timeout_ms)
        try:
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            logger.info("[NAV.open_page] Landed on {!r}", page.url)
        except Exception as exc:
            logger.warning("[NAV.open_page] page.goto raised (non-fatal): {}", str(exc)[:120])
            logger.info("[NAV.open_page] Current URL after goto error: {!r}", page.url)

        return pw, browser, context, page

    except Exception as exc:
        logger.exception("[NAV.open_page] FAILED to open {!r}: {}", url, exc)
        return None


def close_page(pw, browser) -> None:
    logger.debug("[NAV.close_page] Closing browser and Playwright …")
    try:
        browser.close()
        logger.debug("[NAV.close_page] Browser closed")
    except Exception as exc:
        logger.debug("[NAV.close_page] browser.close() error (ignored): {}", exc)
    try:
        pw.stop()
        logger.debug("[NAV.close_page] Playwright stopped")
    except Exception as exc:
        logger.debug("[NAV.close_page] pw.stop() error (ignored): {}", exc)
