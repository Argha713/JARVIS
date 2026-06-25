import asyncio
import json
from loguru import logger

from tools.web_engine import store


class ConnectionManager:
    _HEARTBEAT_TIMEOUT = 5  # seconds to wait for pong before dropping

    def __init__(self):
        self._ws = None
        self._message_handler = None
        self._reconnect_handler = None
        self._event_handlers: dict = {}
        self._last_profile: dict | None = None
        self._heartbeat_interval: int = 20
        self._pong_received: asyncio.Event | None = None

    def configure(self, heartbeat_interval: int = 20):
        self._heartbeat_interval = heartbeat_interval

    # ── Public API ─────────────────────────────────────────────────────────

    def is_connected(self) -> bool:
        return self._ws is not None

    def last_profile(self) -> dict | None:
        return self._last_profile

    def set_message_handler(self, handler):
        """Called by CommandDispatcher to register the response callback."""
        self._message_handler = handler

    def set_reconnect_handler(self, handler):
        """Called by CommandDispatcher to be notified on every extension reconnect.
        handler(exc) is invoked from the event loop with a ConnectionResetError."""
        self._reconnect_handler = handler

    def on_event(self, event_name: str, handler):
        """Register a handler for unsolicited push events from the extension."""
        self._event_handlers[event_name] = handler

    async def send_raw(self, message: dict):
        if self._ws is None:
            raise RuntimeError("No extension connected.")
        await self._ws.send(json.dumps(message))

    # ── Connection lifecycle (called by ws_server per incoming connection) ─

    async def handle_connection(self, websocket):
        if self._ws is not None:
            logger.info("[BrowserExtension] New connection replacing existing one.")

        self._ws = websocket
        self._pong_received = asyncio.Event()
        logger.info("[BrowserExtension] Extension connected.")

        heartbeat = asyncio.create_task(self._heartbeat_loop())
        try:
            await self._read_loop(websocket)
        finally:
            heartbeat.cancel()
            self._ws = None
            self._pong_received = None
            logger.info("[BrowserExtension] Extension disconnected.")
            # Update DB + clear in-memory flag
            try:
                store.extension_state_set_disconnected()
            except Exception as exc:
                logger.debug("[BrowserExtension] extension_state_set_disconnected error: {}", exc)

    # ── Private ────────────────────────────────────────────────────────────

    async def _read_loop(self, websocket):
        async for raw in websocket:
            try:
                data = json.loads(raw)
                await self._route(data)
            except json.JSONDecodeError:
                logger.warning(f"[BrowserExtension] Non-JSON message ignored: {raw!r}")

    async def _route(self, data: dict):
        # Heartbeat pong
        if data.get("status") == "pong":
            logger.debug("[ConnMgr] Received PONG")
            if self._pong_received:
                self._pong_received.set()
            return

        # Response to a dispatched command (has correlation id)
        if "id" in data:
            id_short = data["id"][:8]
            status   = data.get("status", "?")
            logger.debug("[ConnMgr] Routing RESPONSE: id={} status={!r} → message_handler", id_short, status)
            if self._message_handler:
                self._message_handler(data)
            else:
                logger.warning("[ConnMgr] No message_handler set — response dropped for id={}", id_short)
            return

        # Unsolicited push event from extension
        if "event" in data:
            event = data["event"]
            logger.info("[ConnMgr] Received PUSH EVENT: {!r} data={}", event, {k: v for k, v in data.items() if k != "event"})
            if event == "connected":
                profile_dir  = data.get("profile_dir", "Default")
                profile_name = data.get("profile_name", "Default")
                self._last_profile = {
                    "profile_dir":  profile_dir,
                    "profile_name": profile_name,
                }
                logger.info("[ConnMgr] Profile info: {}", self._last_profile)

                # Reject any in-flight commands from the previous WS connection.
                # SW restart closes the old WS, so their Futures will never resolve
                # otherwise — callers would wait the full 30-90s timeout.
                if self._reconnect_handler:
                    try:
                        self._reconnect_handler(ConnectionResetError("Extension reconnected"))
                    except Exception as exc:
                        logger.debug("[ConnMgr] reconnect_handler error (ignored): {}", exc)

                # Persist to DB off the event loop so WebSocket reads aren't blocked.
                loop = asyncio.get_running_loop()
                try:
                    # Infer browser from stored site_profiles or default to "chrome"
                    active  = await loop.run_in_executor(None, store.browser_state_get_active)
                    browser = active["browser"] if active else "chrome"
                    profile = profile_dir or "Default"

                    await loop.run_in_executor(None, store.extension_state_set_connected, browser, profile)
                    await loop.run_in_executor(None, store.browser_state_set_extension_installed, browser, profile, True)
                    await loop.run_in_executor(None, store.browser_state_set_active, browser, profile)
                except Exception as exc:
                    logger.debug("[ConnMgr] State DB update error (ignored): {}", exc)

                # Signal bg_refresher to run immediately
                try:
                    from tools.browser_extension import bg_refresher
                    bg_refresher.signal_refresh_now()
                except Exception as exc:
                    logger.debug("[ConnMgr] bg_refresher signal error (ignored): {}", exc)

            handler = self._event_handlers.get(event)
            if handler:
                logger.debug("[ConnMgr] Dispatching event {!r} to registered handler", event)
                handler(data)
            else:
                logger.debug("[ConnMgr] No handler registered for event {!r}", event)
            return

        logger.warning("[ConnMgr] Unrouted message (no id, no event, not pong): {}", data)

    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            if self._pong_received is None:
                break
            self._pong_received.clear()
            try:
                await self.send_raw({"command": "ping"})
                await asyncio.wait_for(
                    self._pong_received.wait(),
                    timeout=self._HEARTBEAT_TIMEOUT,
                )
                logger.debug("[BrowserExtension] Heartbeat OK.")
            except asyncio.TimeoutError:
                logger.warning("[BrowserExtension] Heartbeat timeout — dropping connection.")
                if self._ws:
                    await self._ws.close()
                break
            except RuntimeError:
                break  # send_raw failed — ws already gone
