from loguru import logger


class ToolRegistry:
    def __init__(self, memory, narration, config: dict):
        from tools.file_ops import FileOps
        from tools.web_search import WebSearch
        from tools.browser import Browser
        from tools.web_engine.engine import WebEngine
        self._tools = {
            "file_ops": FileOps(memory, narration, config),
            "web_search": WebSearch(narration),
            "browser": Browser(narration),
            "web": WebEngine(narration, config),
        }

    def run(self, tool_name: str, params: dict) -> str:
        tool = self._tools.get(tool_name)
        if not tool:
            logger.warning(f"Unknown tool: {tool_name}")
            return f"I don't have a tool called {tool_name} yet, sir."
        logger.info(f"Running tool: {tool_name} params={params}")
        return tool.run(params)
