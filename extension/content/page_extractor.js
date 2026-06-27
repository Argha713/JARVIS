/**
 * page_extractor.js — JARVIS portal section extractor.
 *
 * Injected into background tabs by service_worker.js cmdExtractPage().
 * Exposes window._jarvis.extractPage() which returns [{label, value, selector}].
 *
 * KEEP IN SYNC with _JS in tools/web_engine/extractor.py — same extraction logic.
 */

window._jarvis = window._jarvis || {};

window._jarvis.extractPage = function extractPage() {
    const results = [];
    const seen = new Set();

    function clean(s) {
        return (s || '').trim().replace(/\s+/g, ' ');
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
        if (!value || !/\d/.test(value)) return;

        const titleEl = stat.querySelector('.ant-statistic-title');
        if (titleEl) {
            add(clean(titleEl.innerText), value, stat);
        } else {
            const parent = stat.parentElement;
            if (!parent) return;
            const siblings = Array.from(parent.querySelectorAll('.ant-typography, span[style]'));
            const labelEl = siblings.find(el =>
                !stat.contains(el) &&
                clean(el.innerText).length > 1 &&
                !/^[\d\s%./:-]+$/.test(clean(el.innerText))
            );
            if (labelEl) {
                const label = clean(labelEl.innerText);
                if (label && label !== value) add(label, value, stat);
            }
        }
    });

    // ── Strategy 2: Custom stat cards ──────────────────────────────────────
    document.querySelectorAll('[class*="card"], [class*="stat-card"]').forEach(card => {
        const text = card.innerText;
        if (!text || text.length > 300) return;
        if (!/\d/.test(text)) return;
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
            if (val && /[\d%:]/.test(val) && val.length < 120) {
                add(label, val, el);
            }
        }
    });

    // ── Strategy 4: Table rows ─────────────────────────────────────────────
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

    // ── Strategy 5: Named section headers with numeric content ─────────────
    document.querySelectorAll('[class*="header"], [class*="title"]').forEach(el => {
        const label = clean(el.innerText);
        if (!label || label.length > 80) return;
        let container = el.parentElement;
        for (let i = 0; i < 4 && container; i++) {
            const t = clean(container.innerText);
            if (/\d/.test(t) && t.length < 300 && t !== label) {
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

    // ── Strategy 8: Catch-all (only if nothing found above) ───────────────
    if (results.length === 0) {
        const leafSels = 'p, span, div, td, th, dd, li, h1, h2, h3, h4, h5, h6';
        document.querySelectorAll(leafSels).forEach(el => {
            if (el.children.length > 0) return;
            const text = clean(el.innerText);
            if (!text || text.length < 2 || text.length > 200) return;
            add(text, text, el);
        });
    }

    return results;
};

window._jarvis.discoverNavLinks = function discoverNavLinks() {
    const currentOrigin = window.location.origin;
    const currentPath   = window.location.pathname;
    const seen          = new Set();
    const links         = [];

    // Prefer nav/sidebar/menu areas; fall back to all anchors
    const navEls = document.querySelectorAll(
        'nav, [class*="sidebar"], [class*="menu"], [class*="nav-"], [role="navigation"], header'
    );
    const pool = navEls.length > 0
        ? Array.from(navEls).flatMap(el => Array.from(el.querySelectorAll('a[href]')))
        : Array.from(document.querySelectorAll('a[href]'));

    for (const a of pool) {
        let href;
        try { href = new URL(a.href, window.location.href); } catch { continue; }

        if (href.origin !== currentOrigin)  continue;  // external
        if (href.pathname === currentPath)  continue;  // same page (pagination/filter)
        if (!href.pathname || href.pathname === '/') continue;

        const text  = (a.innerText || '').replace(/\s+/g, ' ').trim();
        const label = a.getAttribute('aria-label') || a.getAttribute('title') || '';

        // Skip bare icon-only links (arrow buttons with no readable text)
        if (!text && !label) continue;

        const normalized = href.origin + href.pathname;
        if (seen.has(normalized)) continue;
        seen.add(normalized);

        links.push({ text: text || label, url: href.href, path: href.pathname });
    }

    return links;
};
