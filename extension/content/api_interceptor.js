// Injected into the MAIN world (not isolated) via executeScript with world:'MAIN'.
// Overrides window.fetch and XMLHttpRequest to capture JSON API responses.
// Results stored in window.__jarvisApiCache, keyed by URL path.
// The service worker reads the cache via a separate MAIN-world executeScript call.

(function () {
    if (window.__jarvisApiCache) return; // already installed

    window.__jarvisApiCache = {};

    const MAX_CACHE_ENTRIES = 50;
    const MAX_BODY_CHARS    = 20_000;

    // Paths to ignore — auth, tokens, health checks, small utility calls
    const SKIP_PATHS = [
        '/auth/', '/login/', '/logout/', '/token', '/refresh',
        '/healthz', '/health', '/metrics', '/favicon',
    ];

    const SKIP_RESPONSE_KEYS = new Set([
        'access_token', 'refresh_token', 'jwt', 'id_token', 'expires_in',
    ]);

    function shouldCapture(url, body) {
        try {
            const path = new URL(url).pathname.toLowerCase();
            if (SKIP_PATHS.some(p => path.includes(p))) return false;
        } catch { return false; }

        if (typeof body !== 'object' || body === null) return false;
        if (Object.keys(body).length < 2) return false; // likely a trivial response
        if (Object.keys(body).some(k => SKIP_RESPONSE_KEYS.has(k))) return false;

        return true;
    }

    function store(url, body) {
        const key = new URL(url).pathname;

        // Evict oldest entry if cache is full
        const keys = Object.keys(window.__jarvisApiCache);
        if (keys.length >= MAX_CACHE_ENTRIES) {
            delete window.__jarvisApiCache[keys[0]];
        }

        window.__jarvisApiCache[key] = {
            url,
            body,
            captured_at: Date.now(),
        };
    }

    // ── Override fetch ──────────────────────────────────────────────────

    const _origFetch = window.fetch.bind(window);
    window.fetch = async function (...args) {
        const response = await _origFetch(...args);
        try {
            const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
            const clone = response.clone();
            const ct = clone.headers.get('content-type') || '';
            if (ct.includes('application/json')) {
                const body = await clone.json().catch(() => null);
                if (body && shouldCapture(url, body)) {
                    store(url, body);
                }
            }
        } catch { /* never break the original request */ }
        return response;
    };

    // ── Override XMLHttpRequest ─────────────────────────────────────────

    const _origOpen = XMLHttpRequest.prototype.open;
    const _origSend = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
        this.__jarvisUrl = url;
        return _origOpen.call(this, method, url, ...rest);
    };

    XMLHttpRequest.prototype.send = function (...args) {
        this.addEventListener('load', function () {
            try {
                const ct = this.getResponseHeader('content-type') || '';
                if (!ct.includes('application/json')) return;
                const body = JSON.parse(this.responseText);
                if (body && shouldCapture(this.__jarvisUrl || '', body)) {
                    store(this.__jarvisUrl, body);
                }
            } catch { /* never break the original request */ }
        });
        return _origSend.call(this, ...args);
    };
})();
