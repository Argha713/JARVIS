import asyncio
import time
import winsound
from pathlib import Path
from loguru import logger


class VoiceOutput:
    def __init__(self, config: dict):
        self.piper_binary = str(Path(config["tts"]["piper_binary"]).resolve())
        self.voice_model = str(Path(config["tts"]["voice_model"]).resolve())
        self.output_wav = str(Path(config["tts"]["output_wav"]).resolve())
        Path(self.output_wav).parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def speak(self, text: str) -> None:
        """Convert text to speech via Piper and play it. Serialised by lock."""
        async with self._lock:
            logger.info(f"[TTS] Speak ({len(text)} chars): {text[:80]!r}")
            wav_path = Path(self.output_wav)

            t0 = time.perf_counter()
            process = await asyncio.create_subprocess_exec(
                self.piper_binary,
                "--model", self.voice_model,
                "--output_file", self.output_wav,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await process.communicate(input=text.encode("utf-8"))
            t_synth = time.perf_counter() - t0
            logger.info(f"[TTS] Piper synthesis: {t_synth:.1f}s")

            if not wav_path.exists():
                logger.error(f"[TTS] Piper did not create WAV: {self.output_wav}")
                return

            loop = asyncio.get_event_loop()
            try:
                t1 = time.perf_counter()
                await loop.run_in_executor(
                    None,
                    winsound.PlaySound,
                    str(wav_path),
                    winsound.SND_FILENAME,
                )
                logger.info(f"[TTS] Playback: {time.perf_counter() - t1:.1f}s")
            except Exception as e:
                logger.error(f"[TTS] Playback failed: {e}")
