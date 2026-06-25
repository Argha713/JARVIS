"""
extract_page — background tab portal extraction via Chrome extension.

Opens a hidden tab for the given URL, waits for React to render, runs
the _jarvis.extractPage() JS extractor, returns all sections, closes tab.

Returns:
    {
        "sections":   [{label, value, selector}, ...],
        "learned_ms": int | None   (None = fixed-wait mode or timed out)
    }

Two modes (controlled by settle_ms):
    settle_ms=None  → polling mode (first visit): polls every 500ms up to 15s.
                       learned_ms = elapsed + 1s buffer (saved to pages.settle_ms).
    settle_ms=N     → fixed-wait mode: waits N ms once, extracts once.
"""
from loguru import logger


async def run(url: str, settle_ms: int | None = None) -> dict:
    from tools.browser_extension import command_dispatcher

    mode = f"fixed-wait settle_ms={settle_ms}" if settle_ms is not None else "polling"
    logger.info("[EXTRACT_PAGE] {} url={!r}", mode, url)

    result = await command_dispatcher.dispatch(
        "extract_page",
        {"url": url, "settle_ms": settle_ms},
        timeout=90,
    )

    data = result.get("data") or {}
    sections   = data.get("sections", [])
    learned_ms = data.get("learned_ms")

    logger.info(
        "[EXTRACT_PAGE] {} section(s) returned learned_ms={}",
        len(sections), learned_ms,
    )
    return {"sections": sections, "learned_ms": learned_ms}
