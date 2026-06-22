"""
Validator: checks stored CSS selectors are still valid.
Runs on startup if any page hasn't been validated in >7 days.
Also triggered by voice: "refresh portal knowledge" / "validate portal data".
"""
from loguru import logger

from tools.web_engine import store
from tools.web_engine.actions import navigate
from tools.web_engine.extractor import refresh_section, extract_page

# Navigation/layout text that only appears in page headers or footers, never in
# real data values.  If a supposedly-extracted value contains these patterns it
# means the selector matched the outermost DOM element and returned the full page.
_GARBAGE_PATTERNS = ("Hello,", "© 20", "EL MINARA", "Simplified HR")

# If this fraction of sections are stale, re-run full extraction to find new selectors.
_RESTALE_THRESHOLD = 0.5


def _is_garbage(value: str) -> bool:
    """Return True if value looks like a full-page body dump instead of real data."""
    if len(value) > 300:
        return True
    # Navigation text present → selector matched the outermost/layout element
    matches = sum(1 for p in _GARBAGE_PATTERNS if p in value)
    return matches >= 2


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
            # Check for login redirect — if the page URL changed, the session may be expired
            final_url = page.url
            if final_url.rstrip("/") != url.rstrip("/"):
                logger.warning(
                    "[VALIDATOR] REDIRECT DETECTED — expected {!r}, landed on {!r}. "
                    "Session may be expired — data below may be wrong.",
                    url, final_url
                )
            else:
                logger.info("[VALIDATOR] Page confirmed at {!r}", final_url)

            # open_page() uses domcontentloaded — React/API-driven components are not
            # rendered yet at that point.  Wait for networkidle so stat cards and tables
            # have finished loading before we check selectors.
            try:
                page.wait_for_load_state("networkidle", timeout=12_000)
                logger.info("[VALIDATOR] networkidle reached — dynamic content ready")
            except Exception:
                logger.warning("[VALIDATOR] networkidle timeout — proceeding anyway")

            sections    = store.get_sections_for_page(page_id)
            stale_count = 0
            fresh_count = 0

            for sec in sections:
                value = refresh_section(page, sec["id"], sec["selector"], sec["label"])

                if value and not _is_garbage(value):
                    logger.info(
                        "[VALIDATOR] FRESH   label={!r:<50} selector={!r:<40} → {!r}",
                        sec["label"][:50], sec["selector"][:40], value[:80]
                    )
                    store.set_cache(sec["id"], value)
                    fresh_count += 1
                else:
                    # Selector broken OR returned full-page garbage — invalidate so the
                    # next query re-discovers a working selector via extract_page().
                    store.invalidate_cache(sec["id"])
                    store.delete_section_embedding(sec["id"])
                    stale_count += 1
                    if value:
                        logger.info(
                            "[VALIDATOR] GARBAGE label={!r:<50} selector={!r:<40} value={!r}",
                            sec["label"][:50], sec["selector"][:40], value[:80]
                        )
                    else:
                        logger.info(
                            "[VALIDATOR] STALE   label={!r:<50} selector={!r}",
                            sec["label"][:50], sec["selector"][:60]
                        )

            total = fresh_count + stale_count
            store.mark_page_validated(page_id)
            logger.info(
                "[VALIDATOR] Done {!r} — {} FRESH, {} STALE/GARBAGE out of {} sections",
                url, fresh_count, stale_count, total
            )

            # When the majority of selectors are broken the page structure has likely
            # changed.  Re-run the full extractor (with the now-loaded page) to
            # discover new, valid selectors and store them for future queries.
            if total > 0 and stale_count / total >= _RESTALE_THRESHOLD:
                logger.info(
                    "[VALIDATOR] {}/{} sections stale — re-extracting {!r} to find new selectors",
                    stale_count, total, url
                )
                new_sections = extract_page(page, site_id, page_id, url)
                logger.info("[VALIDATOR] Re-extraction produced {} new section(s)", len(new_sections))

        finally:
            navigate.close_page(pw, browser)
