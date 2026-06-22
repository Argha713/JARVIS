import asyncio

from loguru import logger

from .connection_manager import ConnectionManager
from .command_dispatcher import CommandDispatcher
from .ws_server import BrowserExtensionServer

# Module-level singletons — shared across the entire JARVIS process.
connection_manager = ConnectionManager()
command_dispatcher = CommandDispatcher(connection_manager)
_server            = BrowserExtensionServer(connection_manager)
_main_loop: asyncio.AbstractEventLoop | None = None


async def start(config: dict):
    """Coroutine passed to asyncio.gather() in main.py. Runs for the lifetime of JARVIS."""
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    logger.info("[BrowserExt] Main event loop captured: {}", id(_main_loop))
    await _server.start(config)


def run_command_sync(command: str, params: dict | None = None, timeout: int = 30) -> dict:
    """
    Run an extension command from a synchronous (non-asyncio) caller.
    Must be called from a worker thread — calling from the asyncio event loop
    thread itself would deadlock. Raises RuntimeError in that case so the caller
    can fall back to Playwright.
    """
    logger.debug("[run_command_sync] ── START command={!r} timeout={}s params={}", command, timeout, params)

    # Check 1: is the extension server up and loop running?
    if _main_loop is None:
        logger.error("[run_command_sync] FAIL — _main_loop is None (start() never called?)")
        raise RuntimeError("Browser extension not running")
    if not _main_loop.is_running():
        logger.error("[run_command_sync] FAIL — _main_loop exists but is not running")
        raise RuntimeError("Browser extension not running")

    logger.debug("[run_command_sync] Loop OK — loop id={} running={}", id(_main_loop), _main_loop.is_running())

    # Check 2: are we on the event loop thread? If so we'd deadlock.
    try:
        running_loop = asyncio.get_running_loop()
        on_event_loop = True
        logger.warning(
            "[run_command_sync] Called from async context (loop id={}) — cannot block; raising so caller can fall back",
            id(running_loop),
        )
    except RuntimeError:
        on_event_loop = False
        logger.debug("[run_command_sync] Called from worker thread — safe to block")

    if on_event_loop:
        raise RuntimeError(
            "run_command_sync called from async context — use "
            "await command_dispatcher.dispatch() instead"
        )

    # Submit to main loop and block until done
    logger.debug("[run_command_sync] Submitting coroutine to main loop via run_coroutine_threadsafe")
    future = asyncio.run_coroutine_threadsafe(
        command_dispatcher.dispatch(command, params or {}, timeout=timeout),
        _main_loop,
    )

    logger.debug("[run_command_sync] Blocking wait (timeout={}s) …", timeout + 5)
    result = future.result(timeout=timeout + 5)

    status    = result.get("status", "?")
    data      = result.get("data")
    data_repr = repr(data)[:120] if data is not None else "None"
    logger.debug("[run_command_sync] ── DONE command={!r} status={!r} data={}", command, status, data_repr)

    return result
