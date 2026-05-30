import asyncio
import time
from core.llm import LLMEngine
from core.narration import Narration
from loguru import logger

MAX_HISTORY_TURNS = 6  # keep last 3 user+assistant pairs
_PASSTHROUGH_PREFIX = "__PASSTHROUGH__:"


class TaskRouter:
    def __init__(self, llm: LLMEngine, narration: Narration, tool_registry=None):
        self.llm = llm
        self.narration = narration
        self.tools = tool_registry
        self._history: list[dict] = []  # conversation memory for current session

    def _add_to_history(self, role: str, content: str) -> None:
        self._history.append({"role": role, "content": content})
        # Keep only last MAX_HISTORY_TURNS messages (pairs of user+assistant)
        if len(self._history) > MAX_HISTORY_TURNS * 2:
            self._history = self._history[-(MAX_HISTORY_TURNS * 2):]

    # Phrases JARVIS uses when asking about a timed-out portal query
    _TIMEOUT_SIGNALS = frozenset({
        "keep checking", "keep an eye", "portal seems slow",
        "slow right now", "let you know when i find",
    })

    _AFFIRMATIONS = frozenset({
        "yes", "yeah", "yep", "sure", "ok", "okay", "please",
        "go ahead", "do it", "absolutely", "definitely", "of course",
    })

    # Portal/HR keywords that indicate a conversation is about portal data
    _PORTAL_SIGNALS = frozenset({
        "punctuality", "attendance", "activity", "leave", "check in", "check out",
        "check-in", "check-out", "portal", "eod", "work journal", "timesheet",
        "payslip", "salary", "seat", "request", "ticket",
    })

    def _is_pending_portal_confirmation(self, user_input: str) -> bool:
        """True if JARVIS just asked about a portal timeout and user is saying yes."""
        import re
        for msg in reversed(self._history[-2:]):
            if msg.get("role") == "assistant":
                content = msg.get("content", "").lower()
                if any(sig in content for sig in self._TIMEOUT_SIGNALS):
                    words = set(re.findall(r'\w+', user_input.lower()))
                    return bool(words & self._AFFIRMATIONS)
        return False

    def _has_portal_context(self) -> bool:
        """True if recent history mentions portal/HR data."""
        for msg in self._history[-4:]:
            content = msg.get("content", "").lower()
            if any(sig in content for sig in self._PORTAL_SIGNALS):
                return True
        return False

    def _pre_route(self, user_input: str) -> dict | None:
        """
        Fast keyword pre-router — checks the resolver before hitting the LLM.
        Also catches temporal follow-ups ("previous month?") when in a portal
        conversation so they don't fall through to the 60s LLM router.
        Returns None if no pre-route match (fall through to LLM routing).
        """
        try:
            from tools.web_engine.resolver import resolve
            site_id, intent = resolve(user_input)
            if site_id:
                logger.info(f"[ROUTER] Pre-routed to web (site={site_id}, intent={intent})")
                return {"tool": "web", "params": {"action": "query", "text": user_input}}
        except Exception as e:
            logger.debug(f"[ROUTER] Pre-route check failed: {e}")

        # Portal timeout confirmation: "yes" after "want me to keep checking?"
        if self._history and self._is_pending_portal_confirmation(user_input):
            logger.info("[ROUTER] Portal timeout confirmation — routing to confirm_pending")
            return {"tool": "web", "params": {"action": "confirm_pending"}}

        # Temporal follow-up: "previous month", "last month", "what about April"
        # Only activate when the active conversation is about portal data
        try:
            from tools.web_engine.discoverer import _extract_month_target
            if self._history and _extract_month_target(user_input) and self._has_portal_context():
                # Trace history to find the site the last portal query used,
                # so engine.run() doesn't re-resolve a bare "previous month?" text
                site_id_hint = None
                from tools.web_engine.resolver import resolve as _web_resolve
                for msg in reversed(self._history[-6:]):
                    if msg.get("role") == "user":
                        sid, _ = _web_resolve(msg.get("content", ""))
                        if sid:
                            site_id_hint = sid
                            break
                params = {"action": "query", "text": user_input}
                if site_id_hint:
                    params["site_id"] = site_id_hint
                logger.info(f"[ROUTER] Temporal follow-up in portal context — routing to web (site={site_id_hint})")
                return {"tool": "web", "params": params}
        except Exception as e:
            logger.debug(f"[ROUTER] Temporal follow-up check failed: {e}")

        return None

    async def handle(self, user_input: str) -> str:
        if not user_input.strip():
            return "I didn't catch that. Could you say that again?"

        logger.info(f"[ROUTER] ── New request ──────────────────────")
        logger.info(f"[ROUTER] User said: {user_input!r}")
        logger.info(f"[ROUTER] History: {len(self._history)} messages in context")

        # Step 1: Route decision — fast pre-router first, then LLM fallback
        t_route = time.perf_counter()
        decision = self._pre_route(user_input)
        if decision is None:
            decision = await self.llm.route(user_input)
        logger.info(f"[ROUTER] Route took {time.perf_counter() - t_route:.1f}s → {decision}")

        tool_name = decision.get("tool", "answer")

        if tool_name == "ollama_offline":
            return "I can't reach my language model right now. Please make sure Ollama is running, sir."

        # Step 2a: Tool path
        if tool_name != "answer" and self.tools:
            params = decision.get("params", {})
            # Always use the original user text for the web tool — LLM paraphrases break tag matching
            if tool_name == "web":
                params["text"] = user_input
            logger.info(f"[ROUTER] Dispatching tool: {tool_name} | params: {params}")
            self.narration.step(f"Using {tool_name.replace('_', ' ')}...")

            t_tool = time.perf_counter()
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self.tools.run, tool_name, params)
            logger.info(f"[ROUTER] Tool done in {time.perf_counter() - t_tool:.1f}s | Result preview: {str(result)[:200]!r}")

            # Passthrough: engine wants JARVIS to speak this verbatim, no LLM summarization
            if str(result).startswith(_PASSTHROUGH_PREFIX):
                response = str(result)[len(_PASSTHROUGH_PREFIX):]
                self._add_to_history("user", user_input)
                self._add_to_history("assistant", response)
                return response

            summary_prompt = (
                f"The user asked: {user_input}\n"
                f"Tool result: {result}\n"
                "In 1-2 spoken sentences (max 40 words), state the key finding. "
                "Quote exact numbers and percentages exactly as given — do not round or rephrase them. "
                "Do not mention any source or website name unless it is explicitly stated in the result."
            )

            t_sum = time.perf_counter()
            # Tool summaries are always short spoken sentences — override style
            response = await self.llm.ask(
                summary_prompt,
                model=self.llm.fast_model,
                style={"num_predict": 100, "length_hint": "Summarise in 1-2 spoken sentences. Mention the source if available."},
            )
            logger.info(f"[ROUTER] Summarise done in {time.perf_counter() - t_sum:.1f}s")

            # Store in history so follow-up questions have context
            self._add_to_history("user", user_input)
            self._add_to_history("assistant", response)
            return response

        # Step 2b: Direct LLM path (with conversation history)
        t_classify = time.perf_counter()
        complexity = await self.llm.classify(user_input)
        logger.info(f"[ROUTER] Classify took {time.perf_counter() - t_classify:.1f}s → {complexity}")

        if complexity == "COMPLEX":
            self.narration.thinking()

        logger.info(f"[ROUTER] Direct LLM [{complexity}]: {user_input!r}")
        t_llm = time.perf_counter()
        response = await self.llm.ask(user_input, history=self._history)
        logger.info(f"[ROUTER] LLM answer done in {time.perf_counter() - t_llm:.1f}s")

        self._add_to_history("user", user_input)
        self._add_to_history("assistant", response)
        return response
