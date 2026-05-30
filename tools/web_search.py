import time
from ddgs import DDGS
from loguru import logger

MAX_RESULTS = 5
MAX_SNIPPET_CHARS = 300


class WebSearch:
    def __init__(self, narration):
        self.narration = narration

    def run(self, params: dict) -> str:
        action = params.get("action", "search")
        query = params.get("query", "")
        if action == "search":
            return self._search(query)
        return f"Unknown web_search action: {action}"

    def _search(self, query: str) -> str:
        self.narration.say(f"Searching the web for {query}...")
        logger.info(f"[WEB] Query: {query!r}")

        t0 = time.perf_counter()
        try:
            results = list(DDGS().text(query, max_results=MAX_RESULTS))
        except Exception as e:
            logger.error(f"[WEB] DDGS failed: {e}")
            return f"Web search failed: {e}"

        elapsed = time.perf_counter() - t0
        logger.info(f"[WEB] Got {len(results)} results in {elapsed:.1f}s")

        if not results:
            return f"No web results found for '{query}'."

        lines = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            body = r.get("body", "")[:MAX_SNIPPET_CHARS]
            url = r.get("href", "")
            logger.debug(f"[WEB] Result {i}: {title!r} | {url}")
            lines.append(f"{i}. {title}\n   {body}\n   Source: {url}")

        return f"Web results for '{query}':\n\n" + "\n\n".join(lines)
