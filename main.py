import asyncio
import json
import re
import sys
import time
import keyboard
from loguru import logger
from core.voice_input import WakeWordListener, CommandRecorder, Transcriber
from core.voice_output import VoiceOutput
from core.narration import Narration
from core.llm import LLMEngine
from core.task_router import TaskRouter
from memory.chroma_store import ChromaStore
from tools.registry import ToolRegistry


# Whisper hallucination phrases emitted on silence/ambient noise
_WHISPER_HALLUCINATIONS = {
    "thanks for watching", "thank you for watching", "thank you", "thanks",
    "you", ".", "..", "...", "subtitles by", "subscribe", "bye",
    "please subscribe", "like and subscribe",
}

def _is_real_speech(text: str) -> bool:
    """Return False if the transcription looks like a Whisper noise hallucination."""
    cleaned = text.strip().lower()
    cleaned_no_punct = re.sub(r"[^\w\s]", "", cleaned).strip()

    # Too short — single word or empty after stripping punctuation
    words = cleaned_no_punct.split()
    if len(words) < 2:
        logger.debug(f"[FILTER] Rejected (too short, {len(words)} word(s)): {text!r}")
        return False

    # Known whisper filler output
    if cleaned_no_punct in _WHISPER_HALLUCINATIONS:
        logger.debug(f"[FILTER] Rejected (known hallucination): {text!r}")
        return False

    # Repeated-phrase detection: "foo bar, foo bar" — Whisper loops on noise
    if len(words) >= 4:
        half = len(words) // 2
        if words[:half] == words[half:half * 2]:
            logger.debug(f"[FILTER] Rejected (repeated phrase): {text!r}")
            return False

    return True


async def main():
    config = json.load(open("config.json"))

    logger.add("logs/jarvis.log", rotation="10 MB", retention="7 days")

    loop = asyncio.get_event_loop()

    tts = VoiceOutput(config)
    narration = Narration(tts, loop)   # loop required for thread-safe narration from tool threads
    llm = LLMEngine(config)
    memory = ChromaStore()
    tool_registry = ToolRegistry(memory, narration, config)
    recorder = CommandRecorder(config)
    transcriber = Transcriber(config)
    router = TaskRouter(llm, narration, tool_registry)

    wake_queue: asyncio.Queue = asyncio.Queue()
    wake_listener = WakeWordListener(loop, wake_queue, config)

    await tts.speak("JARVIS online. Ready when you are, sir.")
    logger.info(
        f"JARVIS started. Wake word: '{config['jarvis']['wake_word']}' | "
        "Hotkey: Ctrl+Space | Terminal: Enter"
    )
    wake_listener.start()

    def _hotkey_fired():
        logger.info("Hotkey triggered (Ctrl+Space).")
        loop.call_soon_threadsafe(wake_queue.put_nowait, "WAKE")

    # suppress=True: consume the keypress so it never reaches the terminal/stdin
    keyboard.add_hotkey("ctrl+space", _hotkey_fired, suppress=True)

    async def keyboard_trigger():
        """Terminal fallback: press Enter to trigger a command cycle."""
        while True:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except Exception as e:
                logger.debug(f"stdin readline error (ignored): {e}")
                await asyncio.sleep(0.1)
                continue
            if line is not None:
                logger.info("Enter key trigger fired.")
                await wake_queue.put("WAKE")

    # How long JARVIS stays in conversation mode after the last response (seconds).
    # User can ask follow-up questions without saying "Hey JARVIS" within this window.
    CONVERSATION_TIMEOUT_SEC = 20

    async def command_processor():
        while True:
            await wake_queue.get()

            wake_listener.stop()
            await asyncio.sleep(0.2)  # let WDM-KS release before recorder opens it

            # ── Conversation session ────────────────────────────────────────────
            # After each response, keep listening for follow-up questions until
            # the user goes silent for CONVERSATION_TIMEOUT_SEC seconds.
            in_conversation = True
            first_turn = True
            while in_conversation:
                try:
                    await tts.speak("Listening...")
                    # First turn: no timeout (user already activated JARVIS).
                    # Follow-up turns: time out if no speech within window.
                    wait_sec = None if first_turn else CONVERSATION_TIMEOUT_SEC
                    audio = await recorder.record(max_wait_sec=wait_sec)
                    first_turn = False
                except Exception as e:
                    logger.exception("Command cycle error")
                    narration.say("Sorry, I had trouble with that.")
                    break

                # Empty array = conversation timeout (no speech detected)
                if audio is not None and len(audio) == 0:
                    logger.info("[CYCLE] Conversation timeout — returning to wake word mode.")
                    narration.say("Standing by, sir.")
                    break

                try:
                    text = await transcriber.transcribe(audio)
                except Exception as e:
                    logger.exception("Transcription error")
                    break

                if text.strip() and _is_real_speech(text):
                    narration.acknowledge()
                    t_start = time.perf_counter()
                    response = await router.handle(text)
                    logger.info(f"[CYCLE] Total processing: {time.perf_counter() - t_start:.1f}s")
                    await tts.speak(response)
                    # Stay in conversation mode for follow-up questions
                else:
                    if text.strip():
                        logger.info(f"[CYCLE] Transcription rejected as noise: {text!r}")
                    narration.say("I didn't catch that.")
                    # Give one more chance before exiting conversation
                    in_conversation = False

            wake_listener.start()

    try:
        await asyncio.gather(
            narration.worker(),
            command_processor(),
            keyboard_trigger(),
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        keyboard.unhook_all()
        wake_listener.stop()
        logger.info("JARVIS shutdown.")


if __name__ == "__main__":
    asyncio.run(main())
