from loguru import logger


class ToolRegistry:
    def __init__(self, memory, narration, config: dict):
        from tools.file_ops import FileOps
        from tools.browser import Browser
        from tools.web_engine.engine import WebEngine
        self._tools = {
            "file_ops":          FileOps(memory, narration, config),
            "browser":           Browser(narration),
            "web":               WebEngine(narration, config),
            "browser_extension": _BrowserExtensionTool(narration, config),
        }

    def run(self, tool_name: str, params: dict) -> str:
        tool = self._tools.get(tool_name)
        if not tool:
            logger.warning(f"Unknown tool: {tool_name}")
            return f"I don't have a tool called {tool_name} yet, sir."
        logger.info(f"Running tool: {tool_name} params={params}")
        return tool.run(params)


class _BrowserExtensionTool:
    """
    Thin registry wrapper for browser-extension-specific commands.
    Currently handles Google search routed by the LLM. Other extension
    commands (navigate, fill_form, etc.) are called directly from
    web_engine actions, not via this wrapper.
    """

    def __init__(self, narration, config: dict):
        self._narration = narration
        self._config    = config

    def run(self, params: dict) -> str:
        action = params.get("action")
        query  = params.get("query", "")

        if action == "search":
            return self._search(query)

        logger.warning("[BrowserExtTool] Unknown action: {!r}", action)
        return f"I don't know how to do {action!r} via the browser extension yet."

    def _search(self, query: str) -> str:
        from tools.browser_extension import connection_manager, run_command_sync

        if not connection_manager.is_connected():
            return self._fallback_web_search(query)

        try:
            result_count = self._config.get("browser_extension", {}).get("search_result_count", 5)
            resp = run_command_sync("search_google", {"query": query, "result_count": result_count})
            data = resp.get("data", {})

            kp      = data.get("knowledge_panel", "")
            results = data.get("results", [])

            if kp:
                return kp

            if results:
                lines = [f"{r['title']}: {r['snippet']}" for r in results if r.get("snippet")]
                return "\n".join(lines[:3])

            return "I searched but couldn't find a clear answer."
        except Exception as exc:
            logger.warning("[BrowserExtTool] Search failed ({}), using fallback", exc)
            return self._fallback_web_search(query)

    def _fallback_web_search(self, query: str) -> str:
        """DuckDuckGo instant answer fallback when extension is offline."""
        try:
            import httpx
            resp = httpx.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_redirect": "1"},
                timeout=8,
            )
            data = resp.json()
            if data.get("AbstractText"):
                return data["AbstractText"]
            if data.get("Answer"):
                return data["Answer"]
            return "I couldn't find a definitive answer right now."
        except Exception as exc:
            logger.warning("[BrowserExtTool] DuckDuckGo fallback also failed: {}", exc)
            return "I couldn't reach the internet right now."
