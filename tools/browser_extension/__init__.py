import asyncio

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
    await _server.start(config)


def run_command_sync(command: str, params: dict | None = None, timeout: int = 30) -> dict:
    """
    Run an extension command from a synchronous (non-asyncio) caller.
    Must be called from a worker thread — calling from the asyncio event loop
    thread itself would deadlock. Raises RuntimeError in that case so the caller
    can fall back to Playwright.
    """
    if _main_loop is None or not _main_loop.is_running():
        raise RuntimeError("Browser extension not running")

    try:
        asyncio.get_running_loop()
        on_event_loop = True
    except RuntimeError:
        on_event_loop = False

    if on_event_loop:
        raise RuntimeError(
            "run_command_sync called from async context — use "
            "await command_dispatcher.dispatch() instead"
        )

    future = asyncio.run_coroutine_threadsafe(
        command_dispatcher.dispatch(command, params or {}, timeout=timeout),
        _main_loop,
    )
    return future.result(timeout=timeout + 5)
