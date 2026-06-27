"""
discover_page — foreground tab extraction for the discovery engine.

Opens the URL in a visible (foreground) tab, waits for React to render,
then extracts sections + nav_links + forms in one shot.
Tab stays open after extraction so the user can see the discovered page.

Returns:
    {
        "sections":   [{label, value, selector}, ...],
        "nav_links":  [{text, url, path}, ...],
        "forms":      {url, fields: [...], submits: [...]},
        "learned_ms": int | None
    }
"""
from loguru import logger


async def run(url: str, settle_ms: int | None = None) -> dict:
    from tools.browser_extension import command_dispatcher

    mode = f"fixed-wait settle_ms={settle_ms}" if settle_ms is not None else "polling"
    logger.info("[DISCOVER_PAGE] {} url={!r}", mode, url)

    result = await command_dispatcher.dispatch(
        "discover_page",
        {"url": url, "settle_ms": settle_ms},
        timeout=90,
    )

    data      = result.get("data") or {}
    sections  = data.get("sections",  [])
    nav_links = data.get("nav_links", [])
    forms     = data.get("forms",     {"fields": [], "submits": []})
    learned_ms = data.get("learned_ms")

    logger.info(
        "[DISCOVER_PAGE] {} section(s) | {} nav_link(s) | {} form_field(s) | learned_ms={}",
        len(sections), len(nav_links), len(forms.get("fields", [])), learned_ms,
    )
    return {"sections": sections, "nav_links": nav_links, "forms": forms, "learned_ms": learned_ms}
