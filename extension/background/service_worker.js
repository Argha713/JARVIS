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

// ── navigate ────────────────────────────────────────────────────────────
// Opens the URL in a background tab (never steals focus from the user).
// If a tab on the same origin already exists and is not active, reuses it.

async function cmdNavigate({ url }) {
    if (!url) throw new Error('navigate: url is required');

    const origin = new URL(url).origin;
    const all = await chrome.tabs.query({});
    const [active] = await chrome.tabs.query({ active: true, currentWindow: true });

    // Prefer an existing same-origin tab that the user is not currently viewing
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
    return { tabId, url };
}

// ── Content-script commands (Step 3) ───────────────────────────────────

async function cmdReadDom(_params) {
    throw new Error('read_dom: not yet implemented (Step 3)');
}

async function cmdReadForm(_params) {
    throw new Error('read_form: not yet implemented (Step 3)');
}

async function cmdFillForm(_params) {
    throw new Error('fill_form: not yet implemented (Step 3)');
}

async function cmdSearchGoogle(_params) {
    throw new Error('search_google: not yet implemented (Step 3)');
}

async function cmdExtractSection(_params) {
    throw new Error('extract_section: not yet implemented (Step 3)');
}

async function cmdGetApiData(_params) {
    throw new Error('get_api_data: not yet implemented (Step 3)');
}

// ── Helpers ─────────────────────────────────────────────────────────────

async function waitForTabLoad(tabId, timeoutMs = 15_000) {
    // Check if the tab is already done loading
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
// MV3 service workers can be killed when idle between heartbeats.
// A periodic alarm wakes the SW so the WebSocket stays connected.

chrome.alarms.create('keepalive', { periodInMinutes: 0.4 }); // ~every 24s

chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name !== 'keepalive') return;
    // Accessing any Chrome API is enough to keep the SW running.
    // ws_client will reconnect automatically if the socket dropped while sleeping.
    chrome.storage.local.get('jarvis_connected', () => {});
});

// ── Boot ────────────────────────────────────────────────────────────────

connect();
