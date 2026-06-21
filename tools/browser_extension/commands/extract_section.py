from .. import command_dispatcher


async def run(
    label: str | None = None,
    selector: str | None = None,
    tab_id: int | None = None,
) -> dict:
    """
    Returns {"found": bool, "text": str}.
    Provide either a label (human text like "Attendance") or a CSS selector.
    """
    params: dict = {}
    if label:
        params["label"] = label
    if selector:
        params["selector"] = selector
    if tab_id is not None:
        params["tabId"] = tab_id
    return await command_dispatcher.dispatch("extract_section", params)
