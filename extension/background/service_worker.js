import { connect, send, onMessage, onOpen, onClose } from './ws_client.js';

// ── Connection state ────────────────────────────────────────────────────

onOpen(() => {
    chrome.storage.local.set({ jarvis_connected: true });
    send({ event: 'connected' });
    console.log('[JARVIS] Extension ready and connected.');
});

onClose(() => {
    chrome.storage.local.set({ jarvis_connected: false });
});

// ── Incoming command routing ────────────────────────────────────────────

onMessage(async (data) => {
    if (data.command === 'ping') {
        send({ status: 'pong' });
        return;
    }

    const { id, command, params } = data;
    if (!id || !command) return;

    try {
        const result = await dispatch(command, params || {});
        send({ id, status: 'ok', data: result });
    } catch (err) {
        console.error(`[JARVIS] '${command}' failed:`, err.message);
        send({ id, status: 'error', error: err.message || String(err) });
    }
});

async function dispatch(command, params) {
    switch (command) {
        case 'navigate':        return cmdNavigate(params);
        case 'read_dom':        return cmdReadDom(params);
        case 'read_form':       return cmdReadForm(params);
        case 'fill_form':       return cmdFillForm(params);
        case 'search_google':   return cmdSearchGoogle(params);
        case 'extract_section': return cmdExtractSection(params);
        case 'get_api_data':    return cmdGetApiData(params);
        case 'extract_page':    return cmdExtractPage(params);
        default:
            throw new Error(`Unknown command: ${command}`);
    }
}

// ── Tab tracking ────────────────────────────────────────────────────────
// Tracks the last tab JARVIS navigated to so DOM commands know where to run.

let _activeTabId = null;

async function getTabId(params = {}) {
    // Caller can explicitly specify a tab
    if (params.tabId) return params.tabId;

    // Use the last tab we navigated to (if it still exists)
    if (_activeTabId) {
        try { await chrome.tabs.get(_activeTabId); return _activeTabId; } catch { _activeTabId = null; }
    }

    // Fall back to the user's active tab
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) throw new Error('No usable tab found.');
    return tab.id;
}

// ── navigate ────────────────────────────────────────────────────────────

async function cmdNavigate({ url }) {
    if (!url) throw new Error('navigate: url is required');

    const origin = new URL(url).origin;
    const all    = await chrome.tabs.query({});
    const [active] = await chrome.tabs.query({ active: true, currentWindow: true });

    // Prefer a non-active tab on the same origin so the user's focus is undisturbed
    const reusable = all.find(t =>
        t.id !== active?.id &&
        t.url &&
        t.url.startsWith(origin)
    );

    let tabId;
    if (reusable) {
        await chrome.tabs.update(reusable.id, { url });
        tabId = reusable.id;
    } else {
        const tab = await chrome.tabs.create({ url, active: false });
        tabId = tab.id;
    }

    await waitForTabLoad(tabId);
    _activeTabId = tabId;

    // Inject the API interceptor into the MAIN world so it can capture fetch/XHR
    await chrome.scripting.executeScript({
        target: { tabId },
        files:  ['content/api_interceptor.js'],
        world:  'MAIN',
    });

    return { tabId, url };
}

// ── read_dom ────────────────────────────────────────────────────────────

async function cmdReadDom(params) {
    const tabId   = await getTabId(params);
    const maxChars = params.maxChars || 8000;

    await injectOnce(tabId, 'content/dom_reader.js');
    const [{ result }] = await chrome.scripting.executeScript({
        target: { tabId },
        func:   (max) => window._jarvis.readDom(max),
        args:   [maxChars],
    });
    return result;
}

// ── read_form ────────────────────────────────────────────────────────────

async function cmdReadForm(params) {
    const tabId = await getTabId(params);

    await injectOnce(tabId, 'content/form_reader.js');
    const [{ result }] = await chrome.scripting.executeScript({
        target: { tabId },
        func:   () => window._jarvis.readForm(),
    });
    return result;
}

// ── fill_form ────────────────────────────────────────────────────────────

async function cmdFillForm({ fields, submit, wait_ms = 500, tabId: explicitTabId }) {
    if (!fields?.length) throw new Error('fill_form: fields array is required');

    const tabId = await getTabId({ tabId: explicitTabId });

    await injectOnce(tabId, 'content/form_filler.js');
    const [{ result }] = await chrome.scripting.executeScript({
        target: { tabId },
        func:   (f, s, w) => window._jarvis.fillForm(f, s, w),
        args:   [fields, submit || null, wait_ms],
    });
    return result;
}

// ── search_google ────────────────────────────────────────────────────────
// Opens a background tab, searches Google, extracts results, closes the tab.

async function cmdSearchGoogle({ query, result_count = 5 }) {
    if (!query) throw new Error('search_google: query is required');

    const url = `https://www.google.com/search?q=${encodeURIComponent(query)}`;
    const tab = await chrome.tabs.create({ url, active: false });
    await waitForTabLoad(tab.id);

    try {
        const [{ result }] = await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            func:   extractGoogleResults,
            args:   [result_count],
        });
        return result;
    } finally {
        await chrome.tabs.remove(tab.id).catch(() => {});
    }
}

function extractGoogleResults(maxResults) {
    const out = { knowledge_panel: '', results: [] };

    // Knowledge panel — direct answers (scores, weather, calculations)
    const kp = document.querySelector(
        '#kp-wp-tab-overview, .kp-wholepage, [data-attrid="title"], .ifM9O'
    );
    if (kp) out.knowledge_panel = kp.innerText.trim().slice(0, 600);

    // Organic results — h3 + cite + snippet inside each result block
    const items = document.querySelectorAll('div.g, [jscontroller][data-hveid]');
    for (const item of items) {
        if (out.results.length >= maxResults) break;
        const h3  = item.querySelector('h3');
        const a   = item.querySelector('a[href]');
        const snip = item.querySelector('.VwiC3b, [data-sncf="1"], .s3v9rd');
        if (!h3 || !a) continue;
        out.results.push({
            title:   h3.innerText.trim(),
            url:     a.href,
            snippet: snip ? snip.innerText.trim().slice(0, 300) : '',
        });
    }
    return out;
}

// ── extract_section ──────────────────────────────────────────────────────
// Finds a specific block of content by label text or CSS selector.

async function cmdExtractSection({ label, selector, tabId: explicitTabId }) {
    const tabId = await getTabId({ tabId: explicitTabId });

    const [{ result }] = await chrome.scripting.executeScript({
        target: { tabId },
        func:   (lbl, sel) => {
            // Try explicit selector first
            if (sel) {
                const el = document.querySelector(sel);
                return el ? { found: true, text: el.innerText.trim() } : { found: false };
            }
            // Search for an element whose text contains the label
            const needle = lbl.toLowerCase();
            for (const el of document.querySelectorAll('[class], [id], td, th, dt, dd, span, div')) {
                const text = el.innerText?.trim().toLowerCase() || '';
                if (text === needle || text.startsWith(needle + ':')) {
                    const sibling = el.nextElementSibling;
                    const value   = sibling?.innerText?.trim() || el.parentElement?.innerText?.trim() || '';
                    if (value) return { found: true, label: lbl, text: value };
                }
            }
            return { found: false };
        },
        args: [label || '', selector || ''],
    });
    return result;
}

// ── get_api_data ─────────────────────────────────────────────────────────
// Reads from the API interceptor cache stored in the MAIN world.

async function cmdGetApiData({ url_pattern, tabId: explicitTabId }) {
    const tabId = await getTabId({ tabId: explicitTabId });

    const [{ result }] = await chrome.scripting.executeScript({
        target: { tabId },
        world:  'MAIN',
        func:   (pattern) => {
            const cache = window.__jarvisApiCache || {};
            if (!pattern) return Object.values(cache);
            const lower = pattern.toLowerCase();
            return Object.values(cache).filter(entry =>
                entry.url.toLowerCase().includes(lower)
            );
        },
        args:   [url_pattern || ''],
    });
    return result;
}

// ── Helpers ─────────────────────────────────────────────────────────────

// Tracks which scripts have been injected per tab to avoid double-injection.
const _injected = new Map(); // tabId → Set<filename>

async function injectOnce(tabId, file) {
    const done = _injected.get(tabId) || new Set();
    if (!done.has(file)) {
        await chrome.scripting.executeScript({ target: { tabId }, files: [file] });
        done.add(file);
        _injected.set(tabId, done);
    }
}

// Clean up injection tracking when a tab is closed
chrome.tabs.onRemoved.addListener((tabId) => {
    _injected.delete(tabId);
    if (_activeTabId === tabId) _activeTabId = null;
});

async function waitForTabLoad(tabId, timeoutMs = 15_000) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status === 'complete') return;

    return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
            chrome.tabs.onUpdated.removeListener(listener);
            reject(new Error(`Tab ${tabId} did not finish loading within ${timeoutMs}ms`));
        }, timeoutMs);

        function listener(id, changeInfo) {
            if (id === tabId && changeInfo.status === 'complete') {
                clearTimeout(timer);
                chrome.tabs.onUpdated.removeListener(listener);
                resolve();
            }
        }
        chrome.tabs.onUpdated.addListener(listener);
    });
}

// ── extract_page — background tab extraction ────────────────────────────
//
// Opens a hidden tab, waits for the page to load + React to render, runs the
// _jarvis.extractPage() extractor, then closes the tab.
//
// Two modes:
//   settle_ms = null  → polling mode (first visit): poll every poll_interval_ms
//                       until sections appear or poll_max_ms elapses.
//                       Returns { sections, learned_ms } where learned_ms = elapsed + 1000.
//   settle_ms = N     → fixed-wait mode (subsequent visits): wait N ms once, extract.
//                       Returns { sections, learned_ms: null }.
//
// KEEP IN SYNC with tools/web_engine/extractor.py (_JS constant) and
// extension/content/page_extractor.js.

async function cmdExtractPage({
    url,
    settle_ms = null,
    poll_interval_ms = 500,
    poll_max_ms = 15_000,
}) {
    if (!url) throw new Error('extract_page: url is required');

    const tab = await chrome.tabs.create({ url, active: false });
    // H7 FIX: persist the tab ID so the next SW startup can close it if the
    // SW is killed before the finally block runs.
    await chrome.storage.session.set({ _jarvis_bg_tab_id: tab.id });

    try {
        await waitForTabLoad(tab.id);
        await injectOnce(tab.id, 'content/page_extractor.js');

        // H8 FIX: an inactive tab has visibilityState='hidden', which causes Ant Design
        // and other lazy-rendering portals to defer stat card rendering entirely.
        // Briefly make the tab active so the page sees a visibility change and renders,
        // then restore the user's previously active tab immediately after.
        const [prevActive] = await chrome.tabs.query({ active: true, currentWindow: true });
        await chrome.tabs.update(tab.id, { active: true });
        await new Promise(r => setTimeout(r, 150));
        if (prevActive) await chrome.tabs.update(prevActive.id, { active: true }).catch(() => {});

        if (settle_ms !== null) {
            // Known settle time — wait once, extract once
            await new Promise(r => setTimeout(r, settle_ms));
            const [{ result }] = await chrome.scripting.executeScript({
                target: { tabId: tab.id },
                // H-LOW FIX: null-guard in case of SPA navigation since injection
                func: () => window._jarvis && window._jarvis.extractPage ? window._jarvis.extractPage() : null,
            });
            return { sections: result || [], learned_ms: null };
        }

        // Polling mode — first visit, unknown render time
        const start = Date.now();
        const deadline = start + poll_max_ms;
        while (Date.now() < deadline) {
            await new Promise(r => setTimeout(r, poll_interval_ms));
            const [{ result }] = await chrome.scripting.executeScript({
                target: { tabId: tab.id },
                func: () => window._jarvis && window._jarvis.extractPage ? window._jarvis.extractPage() : null,
            });
            if (result && result.length > 0) {
                const elapsed = Date.now() - start;
                return { sections: result, learned_ms: elapsed + 1000 };
            }
        }
        // Timed out — page not accessible or session expired
        return { sections: [], learned_ms: null };

    } finally {
        chrome.tabs.remove(tab.id).catch(() => {});
        chrome.storage.session.remove('_jarvis_bg_tab_id').catch(() => {});
    }
}

// ── Keep-alive alarm ────────────────────────────────────────────────────

chrome.alarms.create('keepalive', { periodInMinutes: 0.4 });

chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name !== 'keepalive') return;
    chrome.storage.local.get('jarvis_connected', () => {});
});

// ── Boot ────────────────────────────────────────────────────────────────

// H7 FIX: If the service worker was killed mid-cmdExtractPage, the background
// tab was never closed (the finally block doesn't run in a terminated SW).
// On each SW startup, close any such orphaned tab recorded in session storage.
(async () => {
    try {
        const { _jarvis_bg_tab_id } = await chrome.storage.session.get('_jarvis_bg_tab_id');
        if (_jarvis_bg_tab_id) {
            await chrome.tabs.remove(_jarvis_bg_tab_id).catch(() => {});
            await chrome.storage.session.remove('_jarvis_bg_tab_id');
            console.log('[JARVIS] Closed orphaned background tab', _jarvis_bg_tab_id);
        }
    } catch (e) {}
})();

connect();
