from .. import command_dispatcher


async def run(query: str, result_count: int = 5) -> dict:
    """
    Returns {"knowledge_panel": str, "results": [{"title", "url", "snippet"}, ...]}.
    Opens a background tab, searches Google, closes the tab — no side effects.
    """
    return await command_dispatcher.dispatch(
        "search_google",
        {"query": query, "result_count": result_count},
    )
