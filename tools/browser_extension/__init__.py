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

# Config saved at start() so browser_setup / launcher can read it without threading config through.
_config: dict = {}

# recorder + transcriber registered by main.py after creation so browser_setup can record.
_recorder    = None
_transcriber = None


async def start(config: dict):
    """Coroutine passed to asyncio.gather() in main.py. Runs for the lifetime of JARVIS."""
    global _main_loop, _config
    _main_loop = asyncio.get_running_loop()
    _config    = config
    logger.info("[BrowserExt] Main event loop captured: {} config keys: {}", id(_main_loop), list(config.keys()))
    await _server.start(config)


def register_io(recorder, transcriber) -> None:
    """
    Called by main.py after recorder and transcriber are created.
    Stores references so browser_setup.py can record audio during the profile
    selection window without needing them threaded through every call site.
    """
    global _recorder, _transcriber
    _recorder    = recorder
    _transcriber = transcriber
    logger.info("[BrowserExt] IO registered: recorder={} transcriber={}", type(recorder).__name__, type(transcriber).__name__)


async def ensure_browser_connected(site_id: str, narration) -> bool:
    """
    Async version — call from async context.
    Runs the full browser setup conversation flow.
    """
    from .browser_setup import ensure_connected
    logger.info("[BrowserExt] ensure_browser_connected (async): site_id={!r}", site_id)
    return await ensure_connected(site_id, _config, narration, _recorder, _transcriber)


def ensure_browser_connected_sync(site_id: str, narration, timeout: int = 90) -> bool:
    """
    Sync version for worker threads (registry._search, query_router, etc.).
    Submits ensure_browser_connected() to the main event loop and blocks.
    Returns False if the main loop is not available.
    """
    logger.info("[BrowserExt] ensure_browser_connected_sync: site_id={!r} timeout={}s", site_id, timeout)

    if _main_loop is None or not _main_loop.is_running():
        logger.error("[BrowserExt] ensure_browser_connected_sync: _main_loop not running — returning False")
        return False

    # Detect if called from the event loop thread (would deadlock)
    try:
        asyncio.get_running_loop()
        logger.error("[BrowserExt] ensure_browser_connected_sync called from async context — use await instead")
        return False
    except RuntimeError:
        pass  # correct: we are in a worker thread

    future = asyncio.run_coroutine_threadsafe(
        ensure_browser_connected(site_id, narration),
        _main_loop,
    )
    try:
        result = future.result(timeout=timeout)
        logger.info("[BrowserExt] ensure_browser_connected_sync → {}", result)
        return result
    except Exception as exc:
        logger.error("[BrowserExt] ensure_browser_connected_sync failed: {} {}", type(exc).__name__, exc)
        return False


def run_command_sync(command: str, params: dict | None = None, timeout: int = 30) -> dict:
    """
    Run an extension command from a synchronous (non-asyncio) caller.
    Must be called from a worker thread — calling from the asyncio event loop
    thread itself would deadlock. Raises RuntimeError in that case so the caller
    can fall back to Playwright.
    """
    logger.debug("[run_command_sync] ── START command={!r} timeout={}s params={}", command, timeout, params)

    if _main_loop is None:
        logger.error("[run_command_sync] FAIL — _main_loop is None (start() never called?)")
        raise RuntimeError("Browser extension not running")
    if not _main_loop.is_running():
        logger.error("[run_command_sync] FAIL — _main_loop exists but is not running")
        raise RuntimeError("Browser extension not running")

    logger.debug("[run_command_sync] Loop OK — loop id={} running={}", id(_main_loop), _main_loop.is_running())

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
