"""
Wake word diagnostic — prints live scores and audio RMS so you can see
whether the mic is capturing audio and what score "Hey JARVIS" gets.
Say "Hey JARVIS" a few times. Ctrl+C to stop.
"""
import queue
import numpy as np
import sounddevice as sd
from scipy import signal as scipy_signal
from openwakeword.model import Model

NATIVE_RATE = 48000
TARGET_RATE = 16000
OWW_CHUNK_16K = 1280
NATIVE_CHUNK = int(OWW_CHUNK_16K * NATIVE_RATE / TARGET_RATE)  # 3840

model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
wake_key = list(model.models.keys())[0]
print(f"Model loaded. Prediction key: {wake_key!r}")
print("Say 'Hey JARVIS' — watching scores (Ctrl+C to stop)\n")

audio_q: queue.Queue = queue.Queue()

def callback(indata, frames, time_info, status):
    audio_q.put(indata.copy())

# Find WDM-KS device (same logic as voice_input.py)
wdm_idx = None
for i_api, api in enumerate(sd.query_hostapis()):
    if "WDM-KS" in api["name"]:
        wdm_idx = i_api
        break

device = None
for i, dev in enumerate(sd.query_devices()):
    if dev["hostapi"] != wdm_idx or dev["max_input_channels"] < 1:
        continue
    if dev["default_samplerate"] == 16000:
        continue
    try:
        sd.check_input_settings(device=i, samplerate=NATIVE_RATE, channels=1)
        device = i
        print(f"Using device [{i}] {dev['name']!r}")
        break
    except Exception:
        continue

chunk_count = 0
with sd.InputStream(device=device, samplerate=NATIVE_RATE, channels=1,
                    dtype="int16", blocksize=NATIVE_CHUNK, callback=callback):
    try:
        while True:
            chunk = audio_q.get(timeout=2)
            audio = chunk.flatten()
            rms = np.sqrt(np.mean(audio.astype(np.float32) ** 2))

            resampled = scipy_signal.resample_poly(
                audio, TARGET_RATE, NATIVE_RATE
            ).astype(np.int16)

            prediction = model.predict(resampled)
            score = prediction.get(wake_key, [0])
            score = score[-1] if isinstance(score, list) else score

            chunk_count += 1
            # Print every chunk — show rms so we know mic is live
            bar = "#" * int(score * 40)
            print(f"  rms={rms:6.0f}  score={score:.3f}  {bar}")

    except KeyboardInterrupt:
        print("\nDone.")
