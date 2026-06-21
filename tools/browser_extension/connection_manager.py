import asyncio
import json
from loguru import logger


class ConnectionManager:
    _HEARTBEAT_TIMEOUT = 5  # seconds to wait for pong before dropping

    def __init__(self):
        self._ws = None
        self._message_handler = None
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

    # ── Private ────────────────────────────────────────────────────────────

    async def _read_loop(self, websocket):
        async for raw in websocket:
            try:
                data = json.loads(raw)
                self._route(data)
            except json.JSONDecodeError:
                logger.warning(f"[BrowserExtension] Non-JSON message ignored: {raw!r}")

    def _route(self, data: dict):
        # Heartbeat pong
        if data.get("status") == "pong":
            if self._pong_received:
                self._pong_received.set()
            return

        # Response to a dispatched command (has correlation id)
        if "id" in data:
            if self._message_handler:
                self._message_handler(data)
            return

        # Unsolicited push event from extension
        if "event" in data:
            event = data["event"]
            if event == "connected":
                self._last_profile = {
                    "profile_dir": data.get("profile_dir", ""),
                    "profile_name": data.get("profile_name", ""),
                }
                logger.info(f"[BrowserExtension] Profile: {self._last_profile}")
            handler = self._event_handlers.get(event)
            if handler:
                handler(data)
            return

        logger.debug(f"[BrowserExtension] Unrouted message: {data}")

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
