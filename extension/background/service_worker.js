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

// ── Keep-alive alarm ────────────────────────────────────────────────────

chrome.alarms.create('keepalive', { periodInMinutes: 0.4 });

chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name !== 'keepalive') return;
    chrome.storage.local.get('jarvis_connected', () => {});
});

// ── Boot ────────────────────────────────────────────────────────────────

connect();
