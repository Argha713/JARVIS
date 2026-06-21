from tools.web_engine import retriever


def retrieve(query: str, site_id: str) -> str | None:
    """
    Search ChromaDB for a cached answer. Returns the answer string or None.
    Phase 6: extension-intercepted API data will also be written here via write().
    """
    return retriever.retrieve(query, site_id)


def write(section_id: str, value: str) -> None:
    """Persist a fresh value into the cache (called after extension or Playwright discovery)."""
    from tools.web_engine import store
    store.set_cache(section_id, value)
