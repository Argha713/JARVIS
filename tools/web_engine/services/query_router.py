"""
query_router: decides how to answer a read-intent query.

Decision chain:
  1. API-first (direct httpx — no browser)
  2. ChromaDB cache
       HIT  → return immediately (even if session expired — cached data is still valid)
       MISS → check session_status:
                "expired" → Case 3 (tell user, open login page)
                else      → continue
  3. Extension extract_page (background tab — doesn't disturb user's active tab)
       Sections found → persist to SQLite + ChromaDB → answer from cache
       0 sections     → mark session expired → Case 3
  4. Playwright discoverer (fallback when extension not available)

Returns (answer: str | None, page_url: str | None).
"""
from loguru import logger

from tools.web_engine import store, api_client, discoverer
from tools.web_engine.extractor import persist_sections
from tools.web_engine.services import cache_service, intent_service, portal_seeder

_PORTAL_TIMEOUT = "__PORTAL_TIMEOUT__"

# URL mapping: page_name → path suffix (relative to site base_url)
_PAGE_PATHS: dict[str, str] = {
    "activity": "/my-activity",
    "leave":    "/leave",
    "requests": "/requests",
}


def route(
    query: str,
    site_id: str,
    narration,
    last_page_url: dict[str, str],
    recorder=None,
) -> tuple[str | None, str | None]:
    """
    Returns (answer, page_url).
    answer   = None → caller should return a fallback message.
    page_url = None → discoverer didn't navigate (cache/API hit).
    """
    logger.info("[QueryRouter] ══════════════════════════════════════════")
    logger.info("[QueryRouter] START query={!r} site_id={!r}", query, site_id)

    # ── Fresh detection ───────────────────────────────────────────────────
    needs_fresh = intent_service.detect_fresh_needed(query)
    logger.info("[QueryRouter] needs_fresh={}", needs_fresh)

    # ── Step 1: API-first ─────────────────────────────────────────────────
    page_name = portal_seeder.query_to_page_name(site_id, query)
    logger.info("[QueryRouter] Step 1 — API-first: page_name={!r}", page_name)

    if page_name:
        answer = _try_api(site_id, page_name, query, narration)
        if answer is not None:
            logger.info("[QueryRouter] Step 1 RESOLVED via API")
            logger.info("[QueryRouter] ══════════════════════════════════════════")
            return answer, None
        logger.info("[QueryRouter] Step 1 MISS")
    else:
        logger.info("[QueryRouter] Step 1 SKIP — no page_name mapping")

    # ── Step 2: ChromaDB cache ────────────────────────────────────────────
    logger.info("[QueryRouter] Step 2 — cache: needs_fresh={}", needs_fresh)

    if not needs_fresh:
        answer = cache_service.retrieve(query, site_id)
        if answer:
            logger.info("[QueryRouter] Step 2 RESOLVED via cache")
            logger.info("[QueryRouter] ══════════════════════════════════════════")
            return answer, None
        logger.info("[QueryRouter] Step 2 MISS")

        # Cache miss — check session status before attempting a fresh fetch.
        # A cache HIT bypasses this check: even expired sessions can have valid cached values.
        from tools.browser_extension import bg_refresher as _bgr
        if _bgr.get_session_status(site_id) == "expired":
            logger.info("[QueryRouter] Session expired for {!r} — handling Case 3", site_id)
            return _handle_session_expired(site_id, narration), None
    else:
        logger.info("[QueryRouter] Step 2 SKIP — needs_fresh=True")

    # ── Step 3: Extension extract_page ────────────────────────────────────
    from tools.browser_extension import connection_manager, ensure_browser_connected_sync
    ext_connected = connection_manager.is_connected()
    logger.info("[QueryRouter] Step 3 — extension connected={}", ext_connected)

    if not ext_connected:
        logger.info("[QueryRouter] Step 3 — attempting browser setup for site={!r}", site_id)
        try:
            ext_connected = ensure_browser_connected_sync(site_id, narration)
            logger.info("[QueryRouter] Step 3 — browser setup → connected={}", ext_connected)
        except Exception as exc:
            logger.warning("[QueryRouter] Step 3 — browser setup raised: {}", exc)
            ext_connected = False

    if ext_connected:
        answer = _try_extension_extract_page(query, site_id, page_name, narration)
        if answer:
            logger.info("[QueryRouter] Step 3 RESOLVED via extension extract_page")
            logger.info("[QueryRouter] ══════════════════════════════════════════")
            return answer, None

        # Check if 0 sections flagged a session expiry
        from tools.browser_extension import bg_refresher as _bgr
        if _bgr.get_session_status(site_id) == "expired":
            logger.info("[QueryRouter] Step 3 — extension flagged session expired → Case 3")
            return _handle_session_expired(site_id, narration), None

        logger.info("[QueryRouter] Step 3 MISS")
    else:
        logger.info("[QueryRouter] Step 3 SKIP — extension not available")

    # ── Step 4: Playwright discoverer ─────────────────────────────────────
    site_known = store.site_has_sections(site_id)
    start_url  = last_page_url.get(site_id) if needs_fresh else None

    logger.info("[QueryRouter] Step 4 — Playwright: site_known={} start_url={!r}",
                site_known, start_url)

    if not site_known:
        narration.step("I haven't seen this before — let me find it.")

    answer, page_url = discoverer.discover(
        query, site_id, narration, start_url=start_url, recorder=recorder
    )

    logger.info("[QueryRouter] Step 4 DONE: answer={} page_url={!r}",
                f"len={len(answer)}" if answer and answer != _PORTAL_TIMEOUT else repr(answer),
                page_url)
    logger.info("[QueryRouter] ══════════════════════════════════════════")
    return answer, page_url


# ── Helpers ───────────────────────────────────────────────────────────────────

def _try_api(site_id: str, page_name: str, query: str, narration) -> str | None:
    logger.debug("[QueryRouter._try_api] site={!r} page={!r}", site_id, page_name)
    try:
        api_data = api_client.call_page(site_id, page_name, query)
        if api_data:
            answer = api_client.format_answer(query, api_data)
            if answer:
                return answer
    except api_client.AuthExpired:
        logger.warning("[QueryRouter._try_api] AuthExpired — portal session needs refresh")
        narration.step("The portal session has expired — let me refresh it.")
    except Exception as exc:
        logger.error("[QueryRouter._try_api] Unexpected error: {}: {}", type(exc).__name__, exc)
    return None


def _try_extension_extract_page(
    query: str,
    site_id: str,
    page_name: str | None,
    narration,
) -> str | None:
    """
    Open a background tab via the extension, extract all sections, persist to cache,
    then answer from the newly populated cache.

    Replaces the old navigate → extract_section approach:
      OLD: navigated the user's active tab, extracted one section, didn't update cache
      NEW: opens an invisible background tab, extracts all sections, caches all values
    """
    from tools.browser_extension import run_command_sync

    site = store.get_site(site_id)
    if not site:
        logger.debug("[QueryRouter._try_ext_extract] site not found: {!r}", site_id)
        return None

    target_page = page_name or portal_seeder.query_to_page_name(site_id, query)
    if not target_page:
        logger.debug("[QueryRouter._try_ext_extract] no page mapping for query — cannot extract")
        return None

    base_url = site["base_url"].rstrip("/")
    path     = _PAGE_PATHS.get(target_page, f"/{target_page}")
    url      = base_url + path

    page_row  = store.get_page_by_url(site_id, url)
    settle_ms = page_row["settle_ms"] if page_row and page_row["settle_ms"] else None

    logger.info("[QueryRouter._try_ext_extract] extract_page url={!r} settle_ms={}", url, settle_ms)

    try:
        raw = run_command_sync(
            "extract_page",
            {"url": url, "settle_ms": settle_ms},
            timeout=95,
        )
    except RuntimeError:
        # Called from async context — unexpected, fall through to Playwright
        logger.warning("[QueryRouter._try_ext_extract] RuntimeError (async context?) — skipping")
        return None
    except Exception as exc:
        logger.warning("[QueryRouter._try_ext_extract] extract_page error: {}", exc)
        return None

    data       = raw.get("data") or {}
    sections   = data.get("sections", [])
    learned_ms = data.get("learned_ms")

    logger.info("[QueryRouter._try_ext_extract] {} section(s) returned", len(sections))

    if not sections:
        # 0 sections on a page that previously had data → session likely expired
        if store.site_has_sections(site_id):
            logger.warning(
                "[QueryRouter._try_ext_extract] 0 sections from known site {!r} — "
                "marking session expired", site_id
            )
            store.invalidate_site_cache(site_id)
            store.clear_session(site_id)
            store.site_health_upsert(site_id, "expired", last_section_count=0)
            # Update the in-memory cache in bg_refresher so the next query sees it immediately.
            # H4 FIX: use the public setter (thread-safe, lock-guarded) rather than
            # writing to the private dict directly from this worker thread.
            try:
                from tools.browser_extension import bg_refresher as _bgr
                _bgr.set_session_status(site_id, "expired")
            except Exception:
                pass
        return None

    # Sections found — persist and update state
    if not page_row:
        # First visit: the page hasn't been registered yet. Upsert it now so we
        # have a page_id to associate sections with.
        page_id = store.upsert_page(site_id, url)
        logger.info("[QueryRouter._try_ext_extract] created page_row on first visit: {!r}", url)
    else:
        page_id = page_row["id"]

    meaningful = persist_sections(sections, site_id, page_id, url)
    store.mark_page_validated(page_id)
    if learned_ms:
        store.save_page_settle_ms(page_id, learned_ms)
        logger.info("[QueryRouter._try_ext_extract] learned settle_ms={} for {!r}", learned_ms, url)
    store.site_health_upsert(site_id, "active", last_section_count=len(meaningful))
    logger.info("[QueryRouter._try_ext_extract] {} meaningful section(s) persisted", len(meaningful))

    # Answer from the newly populated cache
    answer = cache_service.retrieve(query, site_id)
    if answer:
        logger.info("[QueryRouter._try_ext_extract] answered from cache after extraction (len={})", len(answer))
    else:
        logger.info("[QueryRouter._try_ext_extract] extraction succeeded but cache_service found no match")
    return answer


def _handle_session_expired(site_id: str, narration) -> str:
    """
    Case 3: session is expired.
    - Open Chrome to the portal login page
    - Start bg_refresher watch mode (retries every 60s for up to 5 min)
    - Return a voice message for JARVIS to say
    """
    site = store.get_site(site_id)
    if not site:
        return "Your portal session has expired, sir. Please log in to the portal and try again."

    base_url  = site["base_url"].rstrip("/")
    login_url = base_url + "/login"

    logger.info("[QueryRouter] Case 3 — session expired for {!r}, opening {!r}", site_id, login_url)

    # Open Chrome and navigate to the login page
    try:
        from tools.browser_extension import connection_manager, run_command_sync, ensure_browser_connected_sync

        if not connection_manager.is_connected():
            logger.info("[QueryRouter] Extension not connected — attempting to open browser")
            ensure_browser_connected_sync(site_id, narration)

        if connection_manager.is_connected():
            run_command_sync("navigate", {"url": login_url}, timeout=15)
            logger.info("[QueryRouter] Navigated to login page {!r}", login_url)
        else:
            logger.warning("[QueryRouter] Could not open browser — user must open it manually")
    except Exception as exc:
        logger.warning("[QueryRouter] Failed to open login page: {}", exc)

    # Schedule watch mode on the main asyncio loop (best-effort).
    # is_watching() is event-loop-only, so submit a coroutine that checks it
    # there — avoids a cross-thread race on the _watching set.
    try:
        import asyncio
        from tools.browser_extension import _main_loop
        from tools.browser_extension import bg_refresher as _bgr
        if _main_loop and _main_loop.is_running():
            asyncio.run_coroutine_threadsafe(
                _bgr.watch_for_session_recovery(site_id, base_url, narration),
                _main_loop,
            )
            logger.info("[QueryRouter] Session watch mode scheduled for {!r}", site_id)
    except Exception as exc:
        logger.debug("[QueryRouter] Could not schedule watch mode: {}", exc)

    return (
        "Your portal session has expired, sir. "
        "I've opened the login page. Please log in and I'll let you know when the portal is ready."
    )


# Expose the sentinel so callers can detect portal timeouts without importing discoverer
PORTAL_TIMEOUT = _PORTAL_TIMEOUT
