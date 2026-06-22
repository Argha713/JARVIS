from loguru import logger

from tools.web_engine import retriever


def retrieve(query: str, site_id: str) -> str | None:
    """
    Search ChromaDB for a cached answer. Returns the answer string or None.
    Phase 6: extension-intercepted API data will also be written here via write().
    """
    logger.debug("[CacheService] retrieve: site={!r} query={!r}", site_id, query[:60])
    answer = retriever.retrieve(query, site_id)
    if answer:
        logger.debug("[CacheService] HIT — answer_len={} preview={!r}", len(answer), answer[:80])
    else:
        logger.debug("[CacheService] MISS — no cached answer for this query")
    return answer


def write(section_id: str, value: str) -> None:
    """Persist a fresh value into the cache (called after extension or Playwright discovery)."""
    from tools.web_engine import store
    logger.debug("[CacheService] write: section_id={!r} value_len={}", section_id[:12], len(value))
    store.set_cache(section_id, value)
    logger.debug("[CacheService] write: saved OK")
