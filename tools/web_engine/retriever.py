"""
Retriever: answers a read query using stored knowledge.

Flow:
  1. Semantic search in ChromaDB for a matching section.
  2. If found and cache fresh  → return cached value immediately (no browser).
  3. If found but cache stale  → re-navigate, re-extract section, update cache.
  4. If not found              → return None (Discoverer handles it).
"""
from loguru import logger

from tools.web_engine import store
from tools.web_engine.actions import navigate
from tools.web_engine.extractor import extract_page, refresh_section

# Minimum semantic similarity to trust a result (0–1).
# 0.35 catches near-matches like "punctuality rate" ↔ "57.89 % Punctuality Rate"
# without dropping to noisy territory.
_MATCH_THRESHOLD = 0.35


def retrieve(query: str, site_id: str) -> str | None:
    """
    Returns the answer string, or None if the data isn't known yet.
    """
    hits = store.semantic_search(query, site_id=site_id, n=5)
    if not hits:
        logger.debug("[RETRIEVER] No ChromaDB hits for {!r}", query[:60])
        return None

    best = hits[0]
    logger.debug("[RETRIEVER] Best hit: label={!r} score={:.2f} url={!r}",
                 best["label"], best["score"], best["url"])

    if best["score"] < _MATCH_THRESHOLD:
        logger.debug("[RETRIEVER] Score {:.2f} below threshold — handing to discoverer",
                     best["score"])
        return None

    section_id = best["section_id"]
    cached = store.get_cache(section_id)
    if cached:
        logger.info("[RETRIEVER] Cache hit for {!r} → {!r}", best["label"], cached[:60])
        return cached

    # Cache stale — re-fetch
    logger.info("[RETRIEVER] Cache stale for {!r} — re-fetching from {!r}",
                best["label"], best["url"])
    return _refresh(site_id, best)


def _refresh(site_id: str, hit: dict) -> str | None:
    """Re-navigate to the page, re-extract the specific section, update cache."""
    result = navigate.open_page(site_id, hit["url"], headless=True)
    if not result:
        return None
    pw, browser, context, page = result
    try:
        # Try fast single-section refresh first
        sec      = store.get_section(hit["section_id"])
        selector = sec["selector"] if sec else ""
        label    = hit["label"]

        value = None
        if selector:
            value = refresh_section(page, hit["section_id"], selector, label)

        if not value:
            # Selector stale — re-run full extractor and search again
            logger.info("[RETRIEVER] Selector stale, running full extractor")
            page_row = store.get_page_by_url(site_id, hit["url"])
            if page_row:
                extract_page(page, site_id, page_row["id"], hit["url"])
                # Re-search after extraction
                hits = store.semantic_search(label, site_id=site_id, n=1)
                if hits and hits[0]["score"] >= _MATCH_THRESHOLD:
                    value = store.get_cache(hits[0]["section_id"])

        if value:
            store.set_cache(hit["section_id"], value)
            logger.info("[RETRIEVER] Refreshed {!r} → {!r}", label, value[:60])
        return value

    finally:
        navigate.close_page(pw, browser)


