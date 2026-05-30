"""
Validator: checks stored CSS selectors are still valid.
Runs on startup if any page hasn't been validated in >7 days.
Also triggered by voice: "refresh portal knowledge" / "validate portal data".
"""
from loguru import logger

from tools.web_engine import store
from tools.web_engine.actions import navigate
from tools.web_engine.extractor import refresh_section


def run_if_due() -> None:
    """Called on JARVIS startup. Runs validation only if pages are overdue."""
    pages = store.pages_needing_validation(max_age_days=7)
    if not pages:
        logger.debug("[VALIDATOR] All pages validated recently — skipping")
        return
    logger.info("[VALIDATOR] {} page(s) due for validation", len(pages))
    _validate_pages(pages)


def run_full(narration=None) -> str:
    """Full manual re-validation triggered by user command."""
    pages = store.pages_needing_validation(max_age_days=0)   # force all
    if not pages:
        return "Everything is already up to date."
    if narration:
        narration.say(
            f"I'll re-validate {len(pages)} page"
            f"{'s' if len(pages) != 1 else ''} in the background, sir."
        )
    _validate_pages(pages)
    return f"Validated {len(pages)} page(s)."


def _validate_pages(pages) -> None:
    for page_row in pages:
        site_id  = page_row["site_id"]
        url      = page_row["url"]
        page_id  = page_row["id"]
        logger.info("[VALIDATOR] Validating {!r}", url)

        result = navigate.open_page(site_id, url, headless=True)
        if not result:
            logger.warning("[VALIDATOR] Could not open {!r} — skipping", url)
            continue

        pw, browser, context, page = result
        try:
            sections = store.get_sections_for_page(page_id)
            stale_count = 0
            for sec in sections:
                value = refresh_section(page, sec["id"], sec["selector"], sec["label"])
                if value:
                    store.set_cache(sec["id"], value)
                else:
                    # Selector broken — invalidate cache + embedding so next query re-discovers
                    store.invalidate_cache(sec["id"])
                    store.delete_section_embedding(sec["id"])
                    stale_count += 1
                    logger.info("[VALIDATOR] Stale section: {!r} on {!r}", sec["label"], url)

            store.mark_page_validated(page_id)
            logger.info("[VALIDATOR] {!r} done — {} stale section(s)", url, stale_count)
        finally:
            navigate.close_page(pw, browser)
