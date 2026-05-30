import asyncio
import json
from core.voice_output import VoiceOutput

async def main():
    config = json.load(open("config.json"))
    tts = VoiceOutput(config)
    await tts.speak("Hello sir, JARVIS is online.")

asyncio.run(main())
