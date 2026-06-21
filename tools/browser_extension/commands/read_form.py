from .. import command_dispatcher


async def run(tab_id: int | None = None) -> dict:
    params = {}
    if tab_id is not None:
        params["tabId"] = tab_id
    return await command_dispatcher.dispatch("read_form", params)
