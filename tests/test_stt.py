"""
Tests the full voice input pipeline from core/voice_input.py:
  1. Finds WDM-KS device automatically
  2. Records until silence
  3. Transcribes with faster-whisper
"""
import asyncio
import json
from core.voice_input import CommandRecorder, Transcriber

async def main():
    config = json.load(open("config.json"))
    recorder = CommandRecorder(config)
    transcriber = Transcriber(config)

    print("Recording until silence (say something, then wait ~1.5s)...")
    audio = await recorder.record()
    print(f"Captured {len(audio)/16000:.1f}s of audio")

    print("Transcribing...")
    text = await transcriber.transcribe(audio)
    print(f"You said: {text!r}")

asyncio.run(main())
