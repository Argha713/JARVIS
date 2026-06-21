import asyncio
import uuid
from loguru import logger
from .connection_manager import ConnectionManager


class CommandDispatcher:
    _DEFAULT_TIMEOUT = 30  # seconds

    def __init__(self, manager: ConnectionManager):
        self._cm = manager
        self._pending: dict[str, asyncio.Future] = {}
        manager.set_message_handler(self._on_response)

    async def dispatch(self, command: str, params: dict = None, timeout: int = None) -> dict:
        """Send a command to the extension and await its response."""
        if not self._cm.is_connected():
            raise RuntimeError("Browser extension not connected.")

        cmd_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = future

        try:
            await self._cm.send_raw({
                "id": cmd_id,
                "command": command,
                "params": params or {},
            })
            return await asyncio.wait_for(future, timeout=timeout or self._DEFAULT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(f"[Dispatcher] Command '{command}' timed out after {timeout or self._DEFAULT_TIMEOUT}s.")
            raise
        finally:
            self._pending.pop(cmd_id, None)

    # ── Called by ConnectionManager when a response arrives ───────────────

    def _on_response(self, data: dict):
        future = self._pending.get(data.get("id", ""))
        if future and not future.done():
            future.set_result(data)
