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
from core import personality
from memory.chroma_store import ChromaStore
from tools.registry import ToolRegistry
from tools.web_engine import store


# Whisper hallucination phrases — populated from DB at boot via personality.boot()
_WHISPER_HALLUCINATIONS: set = set()

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
        
    # Todo - I want to add a functionality to detect if the user is just playing with JARVIS and not actually saying anything meaningful. If the user is just playing with JARVIS, I want to respond with a witty or funny response to keep the interaction engaging. For example, if the user says "blah blah blah" or "lalala", I want JARVIS to respond with something like "I see you're having fun, but let's get back to business!" or "I appreciate your enthusiasm, but let's focus on the task at hand." This will make JARVIS feel more like a friend and less like a robot.
    # Todo - i want to implement this by adding a check for common "nonsense" phrases that people might say when they're just playing around. If the transcribed text matches one of these phrases, I'll have JARVIS respond with a witty comment instead of trying to process it as a command. This will help keep the interaction lighthearted and fun, while also encouraging the user to give real commands when they're ready.
    # ToDo - i want to _WHISPER_HALLUCINATIONS goes from DB. This way, I can easily update the list of known hallucinations without changing the code. I can create a table in the database to store these phrases and have JARVIS query it during startup to populate the _WHISPER_HALLUCINATIONS set. This will make it more flexible and allow me to add new phrases as I discover them or as users report them.
    # ToDo - also the text goes through llm to check if it is a valid command or just nonsense. If it is nonsense, JARVIS will respond with a witty comment. This will help keep the interaction engaging and fun, while also ensuring that JARVIS only processes valid commands.
    # ToDo - if the text is whisper hallucination, add that into db as well, this way we can keep track of new hallucinations that we discover and continuously improve the filtering mechanism. We can have a feedback loop where if JARVIS detects a hallucination that wasn't previously known, it can log it and add it to the database for future reference. This will help make JARVIS more robust over time and reduce false positives from Whisper's noise hallucinations.
    
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

    personality.boot(config)
    _WHISPER_HALLUCINATIONS.update(store.get_hallucinations())

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
                    await tts.speak(personality.say("listening"))
                    #ToDo - I don't want only "Listening..." but also "What can I do for you?" or something like that. Maybe randomize between a few options. JARVIS should be more engaging and less robotic, funny or friendly.
                    # First turn: no timeout (user already activated JARVIS).
                    # Follow-up turns: time out if no speech within window.
                    wait_sec = None if first_turn else CONVERSATION_TIMEOUT_SEC
                    #ToDo - maybe add a shorter timeout for the first turn as well, like 60 seconds,
                    #to avoid waiting indefinitely if something goes wrong with the recorder or the user
                    # walks away after activating JARVIS.
                    #response like "Sorry, I didn't catch that. are you saying something or just playing with me." 
                    #something intersting, friendly, funny, jarvis is users friend. it should be acting like friend.
                    #not the same line every time - feels like a robot, maybe randomize between a few options to keep it engaging.
                    audio = await recorder.record(max_wait_sec=wait_sec)
                    first_turn = False
                except Exception as e:
                    logger.exception("Command cycle error")
                    narration.say(personality.say("error"))
                    break

                # Empty array = conversation timeout (no speech detected)
                if audio is not None and len(audio) == 0:
                    logger.info("[CYCLE] Conversation timeout — returning to wake word mode.")
                    narration.say(personality.say("timeout"))
                    # ToDo - maybe add a witty/friendly response here like "Looks like you went silent, I'll be here when you need me." or "No worries, I'm still here whenever you want to chat." to make it more engaging and less robotic.
                    break

                try:
                    text = await transcriber.transcribe(audio)
                except Exception as e:
                    logger.exception("Transcription error")
                    #Todo - What happened if any error occurs during transcription? Maybe add a friendly/witty response here like "Hmm, I couldn't understand that. Maybe try rephrasing?" or "Sorry, I had trouble understanding. Could you say that again?" to make it more engaging and less robotic.
                    #ToDo - add a friendly/witty response here like "Hmm, I couldn't understand that. Maybe try rephrasing?" or "Sorry, I had trouble understanding. Could you say that again?" to make it more engaging and less robotic.
                    break

                if text.strip() and _is_real_speech(text):
                    narration.acknowledge(personality.say("acknowledge"))
                    # Todo - we set list of ACKNOWLEDGE_PHRASES. i want it to be more dynamic and engaging. 
                    t_start = time.perf_counter()
                    response = await router.handle(text)
                    logger.info(f"[CYCLE] Total processing: {time.perf_counter() - t_start:.1f}s")
                    narration.flush()   # discard stale "Using X..." before speaking answer
                    await tts.speak(response)
                    # Stay in conversation mode for follow-up questions
                else:
                    if text.strip():
                        logger.info(f"[CYCLE] Transcription rejected as noise: {text!r}")
                    narration.say(personality.say("didnt_catch"))
                    # ToDo - maybe add a witty/friendly response here like "Hmm, I couldn't understand that. Maybe try rephrasing?" or "Sorry, I had trouble understanding. Could you say that again?" to make it more engaging and less robotic.
                    # ToDo - randomize the response here to avoid repetition and make JARVIS feel more like a friend and less like a robot. For example, you could have a list of responses like ["Sorry, I didn't catch that. Could you say it again?", "Hmm, I couldn't understand that. Maybe try rephrasing?", "My apologies, I had trouble understanding. Could you repeat that?"] and randomly select one each time to keep the interaction fresh and engaging.
                    # Give one more chance before exiting conversation
                    in_conversation = False

            wake_listener.start()

    try:
        await asyncio.gather(
            narration.worker(),
            command_processor(),
            keyboard_trigger(),
            personality.refresh_loop(llm, config),
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        keyboard.unhook_all()
        wake_listener.stop()
        logger.info("JARVIS shutdown.")


if __name__ == "__main__":
    asyncio.run(main())
