"""
query_router: decides how to answer a read-intent query.

Decision chain:
  1. API-first (direct httpx — no browser)
  2. ChromaDB cache (skip if needs_fresh)
  3. [Phase 6] Browser extension (if connected)
  4. Playwright discoverer (fallback / first-visit / auth-refresh)

Returns (answer: str | None, page_url: str | None).
The caller stores page_url for temporal follow-up hints.
"""
from loguru import logger

from tools.web_engine import store, api_client, discoverer
from tools.web_engine.services import cache_service, intent_service, portal_seeder

_PORTAL_TIMEOUT = "__PORTAL_TIMEOUT__"


def route(
    query: str,
    site_id: str,
    narration,
    last_page_url: dict[str, str],
    recorder=None,
) -> tuple[str | None, str | None]:
    """
    Returns (answer, page_url).
    answer  = None  → caller should return a fallback message.
    page_url = None → discoverer didn't navigate (cache/API hit).
    """
    logger.info("[QueryRouter] ══════════════════════════════════════════")
    logger.info("[QueryRouter] START query={!r} site_id={!r}", query, site_id)

    # ── Fresh detection ───────────────────────────────────────────────────
    needs_fresh = intent_service.detect_fresh_needed(query)
    logger.info("[QueryRouter] needs_fresh={} (time-specific query: {})", needs_fresh, needs_fresh)

    # ── Step 1: API-first ─────────────────────────────────────────────────
    page_name = portal_seeder.query_to_page_name(site_id, query)
    logger.info("[QueryRouter] Step 1 — API-first: page_name={!r}", page_name)

    if page_name:
        logger.debug("[QueryRouter] API path: calling api_client.call_page(site={!r}, page={!r})", site_id, page_name)
        answer = _try_api(site_id, page_name, query, narration)
        if answer is not None:
            logger.info("[QueryRouter] Step 1 RESOLVED via API — returning answer (len={})", len(answer))
            logger.info("[QueryRouter] ══════════════════════════════════════════")
            return answer, None
        logger.info("[QueryRouter] Step 1 MISS — API returned nothing, continuing")
    else:
        logger.info("[QueryRouter] Step 1 SKIP — no page_name mapping for this query (non-portal site or unknown keyword)")

    # ── Step 2: ChromaDB cache ────────────────────────────────────────────
    logger.info("[QueryRouter] Step 2 — ChromaDB cache: needs_fresh={}", needs_fresh)

    if not needs_fresh:
        logger.debug("[QueryRouter] Querying cache for site={!r} query={!r}", site_id, query[:60])
        answer = cache_service.retrieve(query, site_id)
        if answer:
            logger.info("[QueryRouter] Step 2 RESOLVED via cache — returning answer (len={})", len(answer))
            logger.info("[QueryRouter] ══════════════════════════════════════════")
            return answer, None
        logger.info("[QueryRouter] Step 2 MISS — cache has no match, continuing")
    else:
        logger.info("[QueryRouter] Step 2 SKIP — needs_fresh=True, cache would give stale data")

    # ── Step 3: Browser extension ─────────────────────────────────────────
    from tools.browser_extension import connection_manager
    ext_connected = connection_manager.is_connected()
    logger.info("[QueryRouter] Step 3 — Browser extension: connected={}", ext_connected)

    if not ext_connected:
        logger.info("[QueryRouter] Step 3 SKIP — extension not connected")
    else:
        logger.info("[QueryRouter] Step 3 — Extension IS connected but read-path hook not yet wired (Phase 6 TODO)")
        # TODO Phase 6: wire extension read path here
        # answer = _try_extension(query, site_id, page_name, narration)
        # if answer:
        #     return answer, None

    # ── Step 4: Playwright discoverer ─────────────────────────────────────
    site_known     = store.site_has_sections(site_id)
    start_url      = last_page_url.get(site_id) if needs_fresh else None
    cached_page_count = len(last_page_url)

    logger.info(
        "[QueryRouter] Step 4 — Playwright discoverer: site_known={} start_url={!r} last_page_url_cache_size={}",
        site_known, start_url, cached_page_count,
    )

    if not site_known:
        logger.info("[QueryRouter] First visit to this site — JARVIS will narrate 'haven't seen this before'")
        narration.step("I haven't seen this before — let me find it.")
    else:
        logger.debug("[QueryRouter] Site has known sections — no first-visit narration")

    if needs_fresh and start_url:
        logger.info("[QueryRouter] needs_fresh + start_url={!r} → discoverer starts from last known page", start_url)
    elif needs_fresh:
        logger.info("[QueryRouter] needs_fresh=True but no last_url cached for site={!r} — discoverer starts from base", site_id)

    logger.debug("[QueryRouter] Calling discoverer.discover() …")
    answer, page_url = discoverer.discover(
        query, site_id, narration, start_url=start_url, recorder=recorder
    )

    logger.info(
        "[QueryRouter] Step 4 DONE: answer={} page_url={!r}",
        f"len={len(answer)}" if answer and answer != _PORTAL_TIMEOUT else repr(answer),
        page_url,
    )
    logger.info("[QueryRouter] ══════════════════════════════════════════")
    return answer, page_url


# ── Helpers ───────────────────────────────────────────────────────────────────

def _try_api(site_id: str, page_name: str, query: str, narration) -> str | None:
    logger.debug("[QueryRouter._try_api] site={!r} page={!r} query={!r}", site_id, page_name, query[:60])
    try:
        api_data = api_client.call_page(site_id, page_name, query)
        logger.debug("[QueryRouter._try_api] api_data returned: {}", "yes (non-empty)" if api_data else "None/empty")

        if api_data:
            answer = api_client.format_answer(query, api_data)
            logger.debug("[QueryRouter._try_api] format_answer returned: {}", repr(answer)[:80] if answer else "None/empty")
            if answer:
                logger.info("[QueryRouter._try_api] SUCCESS — page={!r} answer_len={}", page_name, len(answer))
                return answer
            logger.info("[QueryRouter._try_api] API had data but format_answer produced empty string")
        else:
            logger.info("[QueryRouter._try_api] api_client returned None (no endpoints / all failed / not a portal site)")

    except api_client.AuthExpired:
        logger.warning("[QueryRouter._try_api] AuthExpired — portal session needs refresh")
        narration.step("The portal session has expired — let me refresh it.")
    except Exception as exc:
        logger.error("[QueryRouter._try_api] Unexpected error: {}: {}", type(exc).__name__, exc)

    return None


# Expose the sentinel so callers can detect portal timeouts without importing discoverer
PORTAL_TIMEOUT = _PORTAL_TIMEOUT
