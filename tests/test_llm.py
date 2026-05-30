import asyncio
import json
from core.llm import LLMEngine

async def main():
    config = json.load(open("config.json"))
    llm = LLMEngine(config)
    print("Testing fast model (phi3)...")
    response = await llm.ask("What is the capital of France?")
    print(f"Response: {response}")

asyncio.run(main())
