from .. import command_dispatcher


async def run(url_pattern: str = "", tab_id: int | None = None) -> list[dict]:
    """
    Returns cached API responses captured by the api_interceptor.
    url_pattern: substring to filter by (empty = return all entries).
    Each entry: {"url": str, "body": dict, "captured_at": int (ms timestamp)}.
    """
    params: dict = {"url_pattern": url_pattern}
    if tab_id is not None:
        params["tabId"] = tab_id
    return await command_dispatcher.dispatch("get_api_data", params)
