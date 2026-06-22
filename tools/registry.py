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
        from tools.browser_extension import connection_manager, run_command_sync, ensure_browser_connected_sync

        logger.info("[BrowserExtTool] _search: query={!r} connected={}", query[:60], connection_manager.is_connected())

        # ── If not connected: attempt browser setup ───────────────────────────
        if not connection_manager.is_connected():
            logger.info("[BrowserExtTool] Extension not connected — attempting browser setup")
            # site_id for a generic search is empty (any profile will do)
            connected = ensure_browser_connected_sync("", self._narration)
            logger.info("[BrowserExtTool] Browser setup result: connected={}", connected)
            if not connected:
                logger.info("[BrowserExtTool] Browser setup failed — using DuckDuckGo fallback")
                return self._fallback_web_search(query)

        # ── Extension is connected — send search_google command ───────────────
        try:
            result_count = self._config.get("browser_extension", {}).get("search_result_count", 5)
            logger.debug("[BrowserExtTool] Sending search_google: query={!r} result_count={}", query[:60], result_count)
            resp = run_command_sync("search_google", {"query": query, "result_count": result_count})
            data = resp.get("data", {})
            status = resp.get("status")
            logger.info("[BrowserExtTool] search_google response: status={!r} data_keys={}", status, list(data.keys()))

            kp      = data.get("knowledge_panel", "")
            results = data.get("results", [])

            if kp:
                logger.info("[BrowserExtTool] Returning knowledge panel (len={})", len(kp))
                return kp

            if results:
                logger.info("[BrowserExtTool] Returning {} result snippets", len(results))
                lines = [f"{r['title']}: {r['snippet']}" for r in results if r.get("snippet")]
                return "\n".join(lines[:3])

            logger.warning("[BrowserExtTool] search_google returned no knowledge_panel or results")
            return "I searched but couldn't find a clear answer."

        except Exception as exc:
            logger.warning("[BrowserExtTool] search_google failed ({}) — using DuckDuckGo fallback", exc)
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
