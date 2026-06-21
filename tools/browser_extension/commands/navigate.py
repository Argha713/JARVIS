from .. import command_dispatcher


async def run(url: str) -> dict:
    return await command_dispatcher.dispatch("navigate", {"url": url})
