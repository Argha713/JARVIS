import asyncio
import websockets
from loguru import logger
from .connection_manager import ConnectionManager


class BrowserExtensionServer:
    def __init__(self, manager: ConnectionManager):
        self._manager = manager

    async def start(self, config: dict):
        cfg = config.get("browser_extension", {})
        port = cfg.get("ws_port", 8765)
        heartbeat_interval = cfg.get("heartbeat_interval_seconds", 20)

        self._manager.configure(heartbeat_interval=heartbeat_interval)

        async with websockets.serve(self._manager.handle_connection, "localhost", port):
            logger.info(f"[BrowserExtension] WebSocket server on ws://localhost:{port}")
            await asyncio.Future()  # run until the event loop is cancelled
