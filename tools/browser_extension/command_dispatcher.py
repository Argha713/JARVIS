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
        t = timeout or self._DEFAULT_TIMEOUT
        logger.debug("[Dispatcher] ── dispatch START command={!r} timeout={}s params={}", command, t, params)

        if not self._cm.is_connected():
            logger.error("[Dispatcher] dispatch FAIL — extension not connected")
            raise RuntimeError("Browser extension not connected.")

        cmd_id    = str(uuid.uuid4())
        id_short  = cmd_id[:8]
        future    = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = future

        logger.debug("[Dispatcher] Created future id={} pending_count={}", id_short, len(self._pending))

        try:
            payload = {"id": cmd_id, "command": command, "params": params or {}}
            logger.debug("[Dispatcher] Sending payload → {}", payload)
            await self._cm.send_raw(payload)
            logger.debug("[Dispatcher] Payload sent — awaiting response (timeout={}s) id={}", t, id_short)

            result = await asyncio.wait_for(future, timeout=t)

            status = result.get("status", "?")
            error  = result.get("error", "")
            data   = result.get("data")
            logger.debug(
                "[Dispatcher] ── dispatch DONE command={!r} id={} status={!r} error={!r} data={}",
                command, id_short, status, error, repr(data)[:120] if data is not None else "None",
            )
            return result

        except asyncio.TimeoutError:
            logger.error("[Dispatcher] TIMEOUT command={!r} id={} after {}s", command, id_short, t)
            raise
        except Exception as exc:
            logger.error("[Dispatcher] ERROR command={!r} id={} — {}: {}", command, id_short, type(exc).__name__, exc)
            raise
        finally:
            removed = self._pending.pop(cmd_id, None)
            logger.debug("[Dispatcher] Cleaned up id={} (was_pending={}) remaining={}", id_short, removed is not None, len(self._pending))

    # ── Called by ConnectionManager when a response arrives ───────────────

    def _on_response(self, data: dict):
        cmd_id   = data.get("id", "")
        id_short = cmd_id[:8] if cmd_id else "?"
        status   = data.get("status", "?")
        logger.debug("[Dispatcher] _on_response: id={} status={!r} pending_count={}", id_short, status, len(self._pending))

        future = self._pending.get(cmd_id)
        if future is None:
            logger.warning("[Dispatcher] _on_response: id={} NOT in pending — stale or duplicate response", id_short)
            return
        if future.done():
            logger.warning("[Dispatcher] _on_response: id={} future already done — ignoring", id_short)
            return

        logger.debug("[Dispatcher] Resolving future id={}", id_short)
        future.set_result(data)
