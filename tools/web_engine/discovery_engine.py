"""
Discovery Engine — given a URL, seeds the DB with site/page/tags/actions.

Called when the user says "watch <URL>". Runs as an asyncio task on the
main event loop so it doesn't block voice interaction.

Phase 1: Navigate to the given URL (foreground tab), extract sections +
         nav_links + forms.
Phase 2: LLM (smart model) analyzes the page — generates site/page/tags/actions.
Phase 3: Crawl discovered nav_links (foreground, 1 level deep, max 20 pages).

DB writes per page:
  - upsert_site      (site_id derived from URL domain)
  - add_tag          (site-level tags → resolver can find site_id)
  - upsert_page      (page_name + url)
  - add_page_tag     (page-level tags → query_router finds correct page)
  - persist_sections (ChromaDB + SQLite cache)
  - upsert_action    (one entry per discovered form)

bg_refresher picks up all upserted pages automatically on its next cycle.
"""
from urllib.parse import urlparse

from loguru import logger

from tools.web_engine import store
from tools.web_engine.site_analyzer import analyze

_MAX_NAV_PAGES = 20


async def discover(url: str, narration, config: dict) -> None:
    """Entry point — called via asyncio.run_coroutine_threadsafe from engine.py."""
    from tools.browser_extension.commands import discover_page as dp_cmd

    # Normalise URL
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed   = urlparse(url)
    site_id  = parsed.netloc.lower()
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    logger.info("[DISCOVERY] ══ START url={!r} site_id={!r}", url, site_id)
    narration.say("Let me explore that page for you, sir.")

    if not store.get_site(site_id):
        store.upsert_site(site_id, base_url)

    # ── Phase 1 ──────────────────────────────────────────────────────────
    logger.info("[DISCOVERY] Phase 1: opening {!r}", url)
    narration.step("Opening the page...")

    try:
        result = await dp_cmd.run(url)
    except Exception as exc:
        logger.error("[DISCOVERY] Phase 1 failed: {}", exc)
        narration.say("I couldn't open that page, sir. Please check the URL and try again.")
        return

    sections   = result.get("sections",  [])
    nav_links  = result.get("nav_links", [])
    forms      = result.get("forms",     {})
    learned_ms = result.get("learned_ms")

    logger.info("[DISCOVERY] Phase 1 done: {} sections, {} nav_links, {} form_fields",
                len(sections), len(nav_links), len(forms.get("fields", [])))

    # ── Phase 2 ──────────────────────────────────────────────────────────
    logger.info("[DISCOVERY] Phase 2: LLM analysis")
    narration.step("Analyzing what I see...")

    analysis = analyze(url, sections, forms, config)
    _write_to_db(site_id, base_url, url, sections, forms, learned_ms, analysis)

    # ── Phase 3 ──────────────────────────────────────────────────────────
    pending = [lnk for lnk in nav_links if lnk.get("url")][:_MAX_NAV_PAGES]

    if pending:
        logger.info("[DISCOVERY] Phase 3: crawling {} nav_link(s)", len(pending))
        narration.step(f"Found {len(pending)} more pages — exploring each one...")

        for i, lnk in enumerate(pending, 1):
            lnk_url = lnk["url"]
            logger.info("[DISCOVERY] Phase 3 [{}/{}]: {!r}", i, len(pending), lnk_url)
            narration.step(f"Exploring page {i} of {len(pending)}...")

            try:
                lnk_result = await dp_cmd.run(lnk_url)
            except Exception as exc:
                logger.warning("[DISCOVERY] Phase 3 skip {!r}: {}", lnk_url, exc)
                continue

            lnk_analysis = analyze(
                lnk_url,
                lnk_result.get("sections",  []),
                lnk_result.get("forms",     {}),
                config,
            )
            _write_to_db(
                site_id, base_url, lnk_url,
                lnk_result.get("sections",  []),
                lnk_result.get("forms",     {}),
                lnk_result.get("learned_ms"),
                lnk_analysis,
            )

    total = 1 + len(pending)
    pages_word = "pages" if total > 1 else "page"
    narration.say(
        f"Discovery complete, sir. I've learned {total} {pages_word} "
        "and set up automatic background refresh."
    )
    logger.info("[DISCOVERY] ══ DONE: {} page(s) for site_id={!r}", total, site_id)


def _write_to_db(
    site_id:   str,
    base_url:  str,
    url:       str,
    sections:  list,
    forms:     dict,
    learned_ms: int | None,
    analysis:  dict,
) -> None:
    site_info = analysis.get("site",    {})
    page_info = analysis.get("page",    {})
    actions   = analysis.get("actions", [])

    # Site name (update if LLM gave a better one)
    if site_info.get("name"):
        store.upsert_site(site_id, base_url, site_info["name"])

    # Site-level tags (for resolver — lets any query reach this site)
    for tag in site_info.get("tags", []):
        store.add_tag(site_id, tag)
    # Also add page tags to site_tags so the resolver finds the site
    for tag in page_info.get("tags", []):
        store.add_tag(site_id, tag)

    # Page record
    page_id = store.upsert_page(site_id, url, page_info.get("name", ""))
    if learned_ms:
        store.save_page_settle_ms(page_id, learned_ms)

    # Page-level tags (for query_router — routes to the right page)
    for tag in page_info.get("tags", []):
        store.add_page_tag(page_id, tag)

    # Sections → ChromaDB + SQLite
    if sections:
        from tools.web_engine.extractor import persist_sections
        persist_sections(sections, site_id, page_id, url)
        store.mark_page_validated(page_id)
        store.site_health_upsert(site_id, "active", last_section_count=len(sections))

    # Actions (one per form)
    for action in actions:
        action_name = (action.get("name") or "").strip()
        if not action_name:
            continue
        action_id = f"{site_id}_{action_name}"
        store.upsert_action(site_id, action_id, {
            "url":             url,
            "fields":          action.get("fields", []),
            "submit_selector": action.get("submit_selector", ""),
            "wait_ms":         action.get("wait_ms", 3000),
        })
        for tag in action.get("tags", []):
            store.add_tag(site_id, tag)
            store.add_page_tag(page_id, tag)

    logger.debug("[DISCOVERY] DB write done for {!r}: {} page_tags, {} actions",
                 url, len(page_info.get("tags", [])), len(actions))
