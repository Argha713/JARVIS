import asyncio
import queue
import threading
import numpy as np
import sounddevice as sd
from scipy import signal as scipy_signal
from openwakeword.model import Model
from faster_whisper import WhisperModel
from loguru import logger

NATIVE_RATE = 48000   # WDM-KS device native rate
TARGET_RATE = 16000   # rate expected by Whisper and openwakeword

_WDM_KS_DEVICE: int | None = -1   # -1 = not yet probed; None = no device found


def _resample(audio: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    return scipy_signal.resample_poly(audio, to_rate, from_rate).astype(np.float32)


def _find_wdm_ks_input_device() -> int | None:
    """
    Returns a WDM-KS input device that actually delivers audio callbacks.
    Result is cached after the first successful probe so startup only probes once.
    """
    global _WDM_KS_DEVICE
    if _WDM_KS_DEVICE != -1:
        return _WDM_KS_DEVICE

    wdm_ks_api_idx = None
    for i_api, api in enumerate(sd.query_hostapis()):
        if "WDM-KS" in api["name"]:
            wdm_ks_api_idx = i_api
            break
    if wdm_ks_api_idx is None:
        _WDM_KS_DEVICE = None
        return None

    for i, dev in enumerate(sd.query_devices()):
        if dev["hostapi"] != wdm_ks_api_idx:
            continue
        if dev["max_input_channels"] < 1:
            continue
        if dev["default_samplerate"] == 16000:
            continue  # 16kHz sub-device hangs in callback mode
        try:
            sd.check_input_settings(device=i, samplerate=NATIVE_RATE, channels=1)
        except Exception:
            continue

        probe_q: queue.Queue = queue.Queue()
        def _probe(indata, frames, t, status):
            probe_q.put(True)
        try:
            with sd.InputStream(device=i, samplerate=NATIVE_RATE, channels=1,
                                dtype="int16", blocksize=4800, callback=_probe):
                probe_q.get(timeout=1.5)
            logger.debug(f"WDM-KS device selected: [{i}] {dev['name']!r} @ {NATIVE_RATE}Hz")
            _WDM_KS_DEVICE = i
            return i
        except Exception as e:
            logger.debug(f"Device [{i}] {dev['name']!r} skipped: {e}")
            continue

    _WDM_KS_DEVICE = None
    return None


class WakeWordListener:
    """
    Runs openwakeword in a background thread using WDM-KS callback-based audio.
    Posts 'WAKE' to wake_queue via call_soon_threadsafe when the wake word is detected.
    """
    DETECTION_THRESHOLD = 0.15
    OWW_CHUNK_16K = 1280                                          # 80ms at 16kHz
    NATIVE_CHUNK = int(OWW_CHUNK_16K * NATIVE_RATE / TARGET_RATE)  # 3840 at 48kHz

    def __init__(self, loop: asyncio.AbstractEventLoop, wake_queue: asyncio.Queue, config: dict):
        self.loop = loop
        self.wake_queue = wake_queue
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        wake_word = config["jarvis"]["wake_word"]
        self.oww_model = Model(wakeword_models=[wake_word], inference_framework="onnx")
        self._wake_word_key = list(self.oww_model.models.keys())[0]
        self._device = None  # probed on each start()

    def start(self) -> None:
        # Re-probe on every start: Playwright/Chromium reconfigures Windows audio
        # routing between command cycles. The device index may stay valid (no -9996)
        # but the endpoint routing changes, degrading wake word scores to ~0.07.
        # A fresh probe picks up the current audio state each time.
        global _WDM_KS_DEVICE
        _WDM_KS_DEVICE = -1
        self._device = _find_wdm_ks_input_device()
        if self._device is None:
            logger.error("[WAKE] No WDM-KS input device found — wake word disabled")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        logger.debug("Wake word listener started (WDM-KS).")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.debug("Wake word listener stopped.")

    def _listen_loop(self) -> None:
        try:
            self._do_listen()
        except Exception as e:
            err = str(e)
            if "-9996" in err or "Invalid device" in err:
                # Playwright/Chromium changed PortAudio's device list — re-probe once
                logger.warning("[WAKE] Device invalid after audio reset (-9996), re-probing...")
                global _WDM_KS_DEVICE
                _WDM_KS_DEVICE = -1
                new_dev = _find_wdm_ks_input_device()
                if new_dev is not None:
                    self._device = new_dev
                    logger.info(f"[WAKE] Re-probed → device {new_dev}, retrying listen loop")
                    try:
                        self._do_listen()
                        return
                    except Exception as e2:
                        logger.error(f"Wake word listener error after re-probe: {e2}")
                else:
                    logger.error("[WAKE] No WDM-KS device found after re-probe")
            else:
                logger.error(f"Wake word listener error: {e}")

    def _do_listen(self) -> None:
        # Reset model state so each listen session starts fresh
        self.oww_model.reset()

        audio_q: queue.Queue = queue.Queue()
        chunk_count = 0

        def callback(indata, frames, time_info, status):
            if not self._stop_event.is_set():
                audio_q.put(indata.copy())

        with sd.InputStream(
            device=self._device,
            samplerate=NATIVE_RATE,
            channels=1,
            dtype="int16",
            blocksize=self.NATIVE_CHUNK,
            callback=callback,
        ):
            while not self._stop_event.is_set():
                try:
                    chunk = audio_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                chunk_count += 1
                chunk_16k = _resample(chunk.flatten(), NATIVE_RATE, TARGET_RATE).astype(np.int16)

                # Log mic RMS every ~2s so we can verify audio is being captured
                if chunk_count % 25 == 0:
                    rms = float(np.sqrt(np.mean(chunk_16k.astype(np.float32) ** 2)))
                    logger.debug(f"[WAKE] Mic RMS: {rms:.0f} (device={self._device})")

                prediction = self.oww_model.predict(chunk_16k)
                scores = prediction.get(self._wake_word_key, [0])
                score = scores[-1] if isinstance(scores, list) else scores
                if score > 0.05:
                    logger.debug(f"Wake word score: {score:.3f}")
                if score > self.DETECTION_THRESHOLD:
                    logger.info(f"Wake word '{self._wake_word_key}' detected (score={score:.2f})")
                    self.loop.call_soon_threadsafe(self.wake_queue.put_nowait, "WAKE")
                    self.oww_model.reset()
                    return


class CommandRecorder:
    """
    Records a spoken command using WDM-KS callback mode (same audio path as
    WakeWordListener). Waits for speech to start, then stops after silence.
    No pyaudio/speech_recognition — avoids the MME conflict with WDM-KS.
    """

    def __init__(self, config: dict):
        self.silence_threshold = config["audio"]["silence_threshold"]
        self.silence_duration = config["audio"]["silence_duration_sec"]
        self._device = _find_wdm_ks_input_device()

    def _record_until_silence(self, max_wait_sec: float = None) -> np.ndarray:
        blocksize = NATIVE_RATE // 10           # 100ms chunks at 48kHz
        chunks_per_sec = NATIVE_RATE / blocksize # = 10.0
        required_silent = int(self.silence_duration * chunks_per_sec)
        max_chunks = int(15 * chunks_per_sec)   # 15s hard cap
        # Conversation timeout: return empty array if no speech starts within max_wait_sec
        wait_cap = int(max_wait_sec * chunks_per_sec) if max_wait_sec else None

        audio_q: queue.Queue = queue.Queue()

        def callback(indata, frames, time_info, status):
            audio_q.put(indata.copy())

        chunks: list = []
        silent_chunks = 0
        speech_started = False

        with sd.InputStream(
            device=self._device,
            samplerate=NATIVE_RATE,
            channels=1,
            dtype="float32",
            blocksize=blocksize,
            callback=callback,
        ):
            logger.debug(f"Recording (threshold={self.silence_threshold})...")
            while True:
                try:
                    chunk = audio_q.get(timeout=10)
                except queue.Empty:
                    logger.warning("Recording timeout — no audio from WDM-KS device")
                    break

                chunks.append(chunk.flatten())
                rms = float(np.sqrt(np.mean(chunk ** 2)))

                if rms >= self.silence_threshold:
                    speech_started = True
                    silent_chunks = 0
                else:
                    if speech_started:
                        silent_chunks += 1

                # Only stop after speech has been detected then gone quiet
                if speech_started and silent_chunks >= required_silent:
                    break
                if len(chunks) >= max_chunks:
                    break
                # Conversation timeout: no speech started within wait_cap chunks
                if wait_cap and not speech_started and len(chunks) >= wait_cap:
                    return np.array([], dtype=np.float32)  # sentinel: timed out

        if not chunks:
            return np.zeros(TARGET_RATE, dtype=np.float32)

        audio = np.concatenate(chunks)
        resampled = _resample(audio, NATIVE_RATE, TARGET_RATE)
        logger.debug(f"Recorded {len(resampled) / TARGET_RATE:.2f}s of audio")
        return resampled

    async def record(self, max_wait_sec: float = None) -> np.ndarray:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._record_until_silence, max_wait_sec)


class Transcriber:
    """
    Transcribes audio using either:
      provider=openai  → whisper-1 API (higher accuracy, proper nouns, ~0.5-1s)
      provider=local   → faster-whisper on CPU (offline fallback, ~0.3-0.8s)

    Set config["whisper"]["provider"] to switch. Falls back to local automatically
    if the OpenAI call fails (network down, quota, etc.).
    """

    def __init__(self, config: dict):
        self._provider = config["whisper"].get("provider", "local")
        self._local_model: WhisperModel | None = None
        self._openai_client = None

        if self._provider == "openai":
            import openai as _openai
            self._openai_client = _openai.OpenAI(
                api_key=config["llm"].get("openai_api_key", "")
            )
            logger.info("[STT] Provider: openai whisper-1")
        else:
            self._load_local(config)

    def _load_local(self, config: dict) -> None:
        if self._local_model is None:
            self._local_model = WhisperModel(
                config["whisper"]["model_size"],
                device=config["whisper"]["device"],
                compute_type=config["whisper"]["compute_type"],
            )
            logger.info("[STT] Provider: faster-whisper ({})", config["whisper"]["model_size"])

    def _transcribe(self, audio: np.ndarray) -> str:
        if self._provider == "openai" and self._openai_client:
            return self._transcribe_openai(audio)
        return self._transcribe_local(audio)

    def _transcribe_local(self, audio: np.ndarray) -> str:
        segments, _ = self._local_model.transcribe(audio, language="en")
        text = " ".join(seg.text for seg in segments).strip()
        logger.debug("[STT] Transcribed (local): {!r}", text)
        return text

    def _transcribe_openai(self, audio: np.ndarray) -> str:
        import os
        import time
        import tempfile
        import wave

        # Write float32 audio to a temp WAV file — OpenAI API requires a file object
        pcm = (audio * 32767).astype(np.int16)

        # Log RMS for calibration — only block truly digital silence (all zeros).
        rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2)))
        logger.debug("[STT] Audio RMS: {:.0f}", rms)
        if rms < 10:
            logger.debug("[STT] Audio is digital silence (rms={:.0f}) — skipping", rms)
            return ""

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp_path = f.name
            with wave.open(tmp_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)       # 16-bit
                wf.setframerate(TARGET_RATE)
                wf.writeframes(pcm.tobytes())

            t0 = time.monotonic()
            with open(tmp_path, "rb") as f:
                result = self._openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    language="en",
                )
            elapsed = time.monotonic() - t0
            text = result.text.strip()
            logger.debug("[STT] Transcribed (openai, {:.2f}s): {!r}", elapsed, text)
            return text

        except Exception as e:
            logger.warning("[STT] OpenAI transcription failed: {} — falling back to local", e)
            if self._local_model:
                return self._transcribe_local(audio)
            return ""
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    async def transcribe(self, audio: np.ndarray) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._transcribe, audio)
