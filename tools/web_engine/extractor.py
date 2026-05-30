"""
Full-page extractor: runs a single JS evaluation on a Playwright page
and returns all meaningful data sections as [{label, value, selector}].
Also stores them in SQLite + ChromaDB.
"""
from loguru import logger
from playwright.sync_api import Page

from tools.web_engine import store

_JS = """
() => {
    const results = [];
    const seen = new Set();

    function clean(s) {
        return (s || '').trim().replace(/\\s+/g, ' ');
    }

    function getSelector(el) {
        if (el.id) return '#' + CSS.escape(el.id);
        const cls = Array.from(el.classList)
            .filter(c => c.length > 1 && !c.match(/^(ant-|d-|mb-|mt-|pb-|pt-|px-|py-|p-|m-|row|col|flex|align|justify|text-|font-|bg-|border-|w-|h-)/))
            .slice(0, 2).join('.');
        const tag = el.tagName.toLowerCase();
        return tag + (cls ? '.' + cls : '');
    }

    function add(label, value, el) {
        label = clean(label);
        value = clean(value);
        if (!label || !value) return;
        if (label.length > 120 || value.length > 400) return;
        const key = label + '||' + value.slice(0, 40);
        if (seen.has(key)) return;
        seen.add(key);
        results.push({ label, value, selector: getSelector(el) });
    }

    // ── Strategy 1: Ant Design statistic cards ─────────────────────────────
    document.querySelectorAll('.ant-statistic').forEach(stat => {
        const valueEl = stat.querySelector('.ant-statistic-content');
        if (!valueEl) return;
        const value = clean(valueEl.innerText);
        if (!value || !/\\d/.test(value)) return;

        const titleEl = stat.querySelector('.ant-statistic-title');
        if (titleEl) {
            // Standard: title lives inside .ant-statistic
            add(clean(titleEl.innerText), value, stat);
        } else {
            // Portal variant: label lives in a sibling element of .ant-statistic
            // (e.g. <div class="ant-space"><span class="ant-typography">Punctuality Rate</span></div>)
            const parent = stat.parentElement;
            if (!parent) return;
            const siblings = Array.from(parent.querySelectorAll('.ant-typography, span[style]'));
            // Pick the first sibling that is NOT a descendant of stat and looks like a label
            const labelEl = siblings.find(el =>
                !stat.contains(el) &&
                clean(el.innerText).length > 1 &&
                !/^[\\d\\s%./:-]+$/.test(clean(el.innerText))
            );
            if (labelEl) {
                const label = clean(labelEl.innerText);
                if (label && label !== value) add(label, value, stat);
            }
        }
    });

    // ── Strategy 2: Custom stat cards (any [class*=card] with a number) ────
    document.querySelectorAll('[class*="card"], [class*="stat-card"]').forEach(card => {
        const text = card.innerText;
        if (!text || text.length > 300) return;
        if (!/\\d/.test(text)) return;
        // Only leaf-like cards (no child cards)
        if (card.querySelector('[class*="card"]')) return;
        add(text, text, card);
    });

    // ── Strategy 3: Explicit label + sibling value pairs ───────────────────
    document.querySelectorAll('h4, h3, p, span, label').forEach(el => {
        const label = clean(el.innerText);
        if (!label || label.length > 80 || /[<>{}]/.test(label)) return;
        const sib = el.nextElementSibling;
        if (sib) {
            const val = clean(sib.innerText);
            if (val && /[\\d%:]/.test(val) && val.length < 120) {
                add(label, val, el);
            }
        }
    });

    // ── Strategy 4: Table rows (requests, timesheets) ──────────────────────
    document.querySelectorAll(
        '.ant-table-tbody tr, table tbody tr, [class*="table"] tr'
    ).forEach(row => {
        const cells = Array.from(row.querySelectorAll('td'))
            .map(td => clean(td.innerText))
            .filter(Boolean);
        if (cells.length >= 2) {
            const text = cells.join(' | ');
            if (text.length < 300) add(text, text, row);
        }
    });

    // ── Strategy 5: Named section headers with any numeric content ─────────
    document.querySelectorAll('[class*="header"], [class*="title"]').forEach(el => {
        const label = clean(el.innerText);
        if (!label || label.length > 80) return;
        let container = el.parentElement;
        for (let i = 0; i < 4 && container; i++) {
            const t = clean(container.innerText);
            if (/\\d/.test(t) && t.length < 300 && t !== label) {
                add(label, t, container);
                break;
            }
            container = container.parentElement;
        }
    });

    // ── Strategy 6: Ant Design card bodies ────────────────────────────────
    document.querySelectorAll('.ant-card').forEach(card => {
        const head = card.querySelector('.ant-card-head-title');
        const body = card.querySelector('.ant-card-body');
        if (!body) return;
        const bodyText = clean(body.innerText);
        if (!bodyText || bodyText.length > 400) return;
        const label = head ? clean(head.innerText) : bodyText.slice(0, 60);
        if (label) add(label, bodyText, body);
    });

    // ── Strategy 7: dt/dd definition pairs ────────────────────────────────
    document.querySelectorAll('dt').forEach(dt => {
        const dd = dt.nextElementSibling;
        if (dd && dd.tagName === 'DD') {
            add(dt.innerText, dd.innerText, dt);
        }
    });

    // ── Strategy 8: Catch-all — all leaf text nodes with data ─────────────
    // Only runs if nothing found above (avoids noise on data-rich pages)
    if (results.length === 0) {
        const leafSels = 'p, span, div, td, th, dd, li, h1, h2, h3, h4, h5, h6';
        document.querySelectorAll(leafSels).forEach(el => {
            if (el.children.length > 0) return;   // leaf nodes only
            const text = clean(el.innerText);
            if (!text || text.length < 2 || text.length > 200) return;
            add(text, text, el);
        });
    }

    return results;
}
"""


def extract_page(page: Page, site_id: str, page_id: str, url: str) -> list[dict]:
    """
    Run the full-page extractor on a loaded Playwright page.
    Returns list of {label, value, selector}.
    Also persists everything to SQLite + ChromaDB.
    """
    logger.info("[EXTRACTOR] Extracting: {} (site={})", url, site_id)
    try:
        raw = page.evaluate(_JS)
    except Exception as e:
        logger.warning("[EXTRACTOR] JS evaluation failed: {}", e)
        return []

    if not raw:
        # Diagnostic: dump a sample of visible page text so we can debug selectors
        try:
            sample = page.evaluate(
                "() => document.body.innerText.trim().replace(/\\s+/g,' ').slice(0, 500)"
            )
            logger.warning("[EXTRACTOR] No sections found on {} | Page text sample: {}",
                           url, sample[:300] if sample else "(empty)")
        except Exception:
            pass
        return []

    # Deduplicate by label (keep first occurrence)
    seen_labels: set = set()
    sections = []
    for item in raw:
        lbl = item.get("label", "").strip()
        if not lbl or lbl in seen_labels:
            continue
        seen_labels.add(lbl)
        sections.append(item)

    logger.info("[EXTRACTOR] {} sections found on: {}", len(sections), url)

    # Log every extracted section so we can see exactly what was picked up
    for i, item in enumerate(sections):
        logger.debug(
            "[EXTRACTOR] #{:02d} label={!r:<45} value={!r}",
            i + 1, item["label"][:45], item["value"][:60]
        )

    # Persist
    new_labels: set[str] = set()
    for item in sections:
        label    = item["label"]
        value    = item["value"]
        selector = item.get("selector", "")
        new_labels.add(label)

        section_id = store.upsert_section(page_id, label, selector)
        store.index_section(section_id, label, value, site_id, page_id, url)
        logger.debug("[EXTRACTOR] Indexed  {} label={!r} value={!r}",
                     section_id[:8], label[:40], value[:40])

    # Remove stale ChromaDB entries for sections no longer found on this page.
    # This prevents old value-embedded labels (e.g. "57.89 % Punctuality Rate") from
    # persisting in the index after the extractor learns the clean label ("Punctuality Rate").
    existing = store.get_sections_for_page(page_id)
    stale_removed = 0
    for sec in existing:
        if sec["label"] not in new_labels:
            store.delete_section_embedding(sec["id"])
            stale_removed += 1
            logger.debug("[EXTRACTOR] Purged stale ChromaDB entry: {!r}", sec["label"][:60])

    logger.info("[EXTRACTOR] Done — {} indexed, {} stale purged from ChromaDB",
                len(sections), stale_removed)
    return sections


def refresh_section(page: Page, section_id: str, selector: str, label: str) -> str | None:
    """
    Re-extract a single known section using its stored selector.
    Returns the fresh value string, or None if selector no longer works.
    """
    try:
        el = page.query_selector(selector)
        if not el:
            logger.debug("[EXTRACTOR] Selector {!r} not found for {!r}", selector, label)
            return None
        value = el.inner_text().strip().replace("\n", " ").replace("\t", " ")
        value = " ".join(value.split())
        logger.debug("[EXTRACTOR] Refreshed {!r} → {!r}", label, value[:60])
        return value or None
    except Exception as e:
        logger.debug("[EXTRACTOR] refresh_section error for {!r}: {}", label, e)
        return None
