// WebSocket client with exponential backoff reconnect.
// Runs inside the MV3 service worker — imported as an ES module.

const WS_URL = 'ws://localhost:8765';
const MIN_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 30_000;

let _ws = null;
let _backoff = MIN_BACKOFF_MS;
let _onMessage = null;
let _onOpen = null;
let _onClose = null;

function connect() {
    if (_ws && (_ws.readyState === WebSocket.CONNECTING || _ws.readyState === WebSocket.OPEN)) {
        return;
    }

    console.log(`[JARVIS] Connecting to ${WS_URL}…`);
    _ws = new WebSocket(WS_URL);

    _ws.onopen = () => {
        console.log('[JARVIS] Connected.');
        _backoff = MIN_BACKOFF_MS;
        if (_onOpen) _onOpen();
    };

    _ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            if (_onMessage) _onMessage(data);
        } catch {
            console.warn('[JARVIS] Non-JSON message ignored:', event.data);
        }
    };

    _ws.onclose = () => {
        console.log(`[JARVIS] Disconnected. Reconnecting in ${_backoff}ms…`);
        _ws = null;
        if (_onClose) _onClose();
        setTimeout(() => {
            _backoff = Math.min(_backoff * 2, MAX_BACKOFF_MS);
            connect();
        }, _backoff);
    };

    _ws.onerror = () => {
        // onclose fires after onerror — reconnect handled there.
    };
}

function send(message) {
    if (_ws && _ws.readyState === WebSocket.OPEN) {
        _ws.send(JSON.stringify(message));
        return true;
    }
    return false;
}

function isConnected() {
    return _ws !== null && _ws.readyState === WebSocket.OPEN;
}

function onMessage(cb) { _onMessage = cb; }
function onOpen(cb)    { _onOpen    = cb; }
function onClose(cb)   { _onClose   = cb; }

export { connect, send, isConnected, onMessage, onOpen, onClose };
