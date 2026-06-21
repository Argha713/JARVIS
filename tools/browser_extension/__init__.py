from .connection_manager import ConnectionManager
from .command_dispatcher import CommandDispatcher
from .ws_server import BrowserExtensionServer

# Module-level singletons — shared across the entire JARVIS process.
connection_manager = ConnectionManager()
command_dispatcher = CommandDispatcher(connection_manager)
_server = BrowserExtensionServer(connection_manager)


async def start(config: dict):
    """Coroutine passed to asyncio.gather() in main.py. Runs for the lifetime of JARVIS."""
    await _server.start(config)
