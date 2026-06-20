import asyncio
from loguru import logger


class Narration:
    def __init__(self, tts, loop: asyncio.AbstractEventLoop = None):
        self.tts   = tts
        self.queue: asyncio.Queue = asyncio.Queue()
        self._loop = loop  # required for thread-safe say() calls from tool threads

    def say(self, message: str) -> None:
        """
        Non-blocking. Thread-safe when a loop was supplied at construction.
        asyncio.Queue is NOT thread-safe — put_nowait() from a thread pool thread
        (e.g. run_in_executor) never wakes the event loop, so messages pile up
        silently until the executor returns. call_soon_threadsafe() fixes that.
        """
        logger.debug(f"Narration queued: {message}")
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.queue.put_nowait, message)
        else:
            self.queue.put_nowait(message)

    def acknowledge(self, phrase: str = "On it.") -> None:
        self.say(phrase)

    def thinking(self) -> None:
        self.say("Let me think about that...")

    def step(self, action: str) -> None:
        self.say(action)

    def flush(self) -> None:
        """
        Discard all pending queued narration items.
        Call this just before speaking the final answer so stale progress messages
        (e.g. 'Searching the web...') don't play out after the answer has been given.
        """
        count = 0
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
                count += 1
            except Exception:
                break
        if count:
            logger.debug("[NARRATION] Flushed {} stale item(s)", count)

    async def worker(self) -> None:
        """Long-running coroutine. Drains queue and speaks each message in order."""
        while True:
            message = await self.queue.get()
            await self.tts.speak(message)
            self.queue.task_done()
