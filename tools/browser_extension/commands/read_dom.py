from .. import command_dispatcher


async def run(tab_id: int | None = None, max_chars: int = 8000) -> dict:
    params = {"maxChars": max_chars}
    if tab_id is not None:
        params["tabId"] = tab_id
    return await command_dispatcher.dispatch("read_dom", params)
