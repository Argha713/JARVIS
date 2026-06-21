from .. import command_dispatcher


async def run(
    fields: list[dict],
    submit: str | None = None,
    wait_ms: int = 500,
    tab_id: int | None = None,
) -> dict:
    """
    fields: list of {"selector": "...", "value": "..."} dicts
    submit: CSS selector for the submit button (optional — pass None to skip)
    """
    params: dict = {"fields": fields, "wait_ms": wait_ms}
    if submit:
        params["submit"] = submit
    if tab_id is not None:
        params["tabId"] = tab_id
    return await command_dispatcher.dispatch("fill_form", params)
