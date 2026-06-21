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
    needs_fresh = intent_service.detect_fresh_needed(query)
    _log_fresh_decision(query, needs_fresh)

    # ── 1. API-first (httpx direct call) ─────────────────────────────────
    page_name = portal_seeder.query_to_page_name(site_id, query)
    if page_name:
        answer = _try_api(site_id, page_name, query, narration)
        if answer is not None:
            return answer, None

    # ── 2. ChromaDB cache ─────────────────────────────────────────────────
    if not needs_fresh:
        answer = cache_service.retrieve(query, site_id)
        if answer:
            logger.debug("[QueryRouter] Cache HIT → returning cached answer")
            return answer, None
        logger.debug("[QueryRouter] Cache MISS → continuing")
    else:
        logger.debug("[QueryRouter] needs_fresh=True → cache skipped")

    # ── 3. Browser extension (Phase 6 hook) ───────────────────────────────
    # TODO Phase 6 Step 6: wire extension read path here.
    # from tools.browser_extension import connection_manager
    # if connection_manager.is_connected():
    #     answer = _try_extension(query, site_id, page_name, narration)
    #     if answer:
    #         return answer, None

    # ── 4. Playwright discoverer ──────────────────────────────────────────
    narration.step("I haven't seen this before — let me find it." if not store.site_has_sections(site_id) else "")
    start_url = last_page_url.get(site_id) if needs_fresh else None
    if start_url:
        logger.info("[QueryRouter] needs_fresh — passing start_url={} to discoverer", start_url)

    answer, page_url = discoverer.discover(
        query, site_id, narration, start_url=start_url, recorder=recorder
    )
    return answer, page_url


# ── Helpers ───────────────────────────────────────────────────────────────────

def _try_api(site_id: str, page_name: str, query: str, narration) -> str | None:
    logger.debug("[QueryRouter] API-first: page_name={!r}", page_name)
    try:
        api_data = api_client.call_page(site_id, page_name, query)
        if api_data:
            answer = api_client.format_answer(query, api_data)
            if answer:
                logger.info("[QueryRouter] API-first answered page={!r}", page_name)
                return answer
            logger.debug("[QueryRouter] API returned data but format_answer was empty")
        else:
            logger.debug("[QueryRouter] API returned None → falling through")
    except api_client.AuthExpired:
        logger.info("[QueryRouter] AuthExpired → session needs refresh")
        narration.step("The portal session has expired — let me refresh it.")
    return None


def _log_fresh_decision(query: str, needs_fresh: bool) -> None:
    if needs_fresh:
        logger.debug("[QueryRouter] needs_fresh=True → will skip cache, use discoverer")
    else:
        logger.debug("[QueryRouter] needs_fresh=False → will try API, cache, discoverer")


# Expose the sentinel so callers can detect portal timeouts without importing discoverer
PORTAL_TIMEOUT = _PORTAL_TIMEOUT
