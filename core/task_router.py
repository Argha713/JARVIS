import asyncio
import json
import re
import time
from core.llm import LLMEngine
from core.narration import Narration
from core import personality
from loguru import logger

MAX_HISTORY_TURNS = 6  # keep last 3 user+assistant pairs
_PASSTHROUGH_PREFIX = "__PASSTHROUGH__:"
_CONFIG_PATH = "config.json"

# Regex to detect provider-switch voice commands
_SWITCH_RE = re.compile(
    r'\b(switch|change|use|set)\b.{0,20}\b(openai|open\s*ai|ollama)\b',
    re.IGNORECASE,
)

# Fast gate for procedure management — avoids LLM call on ordinary queries
_MGMT_HINTS = frozenset([
    "forget", "delete", "remove", "stop remembering", "don't remember",
    "next time use", "next time do", "redo", "that was wrong",
    "use different", "change how", "update how",
])

# EOD submission pattern — same as engine.py
_EOD_RE = re.compile(
    r"(?:submit|send|log|write|record)\s+(?:my\s+)?(?:eod|end[- ]of[- ]day|work journal)[:\s]+(.+)",
    re.I,
)

# Phase 5.5: multi-turn site teaching patterns
_TAG_TEACH_RE = re.compile(r'\bwhen\s+i\s+say\b', re.IGNORECASE)

# Discovery: "watch <url>", "start watching <url>", "jarvis watch <url>"
_WATCH_RE = re.compile(
    r'\b(?:watch|start\s+watching|monitor|track|add\s+page|learn\s+page)\b'
    r'.{0,30}(https?://\S+|\b[\w.-]+\.(?:com|org|net|io|in|co\.in)\S*)',
    re.IGNORECASE,
)
_REFRESH_RE   = re.compile(
    r'\b(refresh|update|re-?learn|rebuild|sync)\b.{0,25}\b(portal|knowledge|web|site|data)\b'
    r'|\blearn\s+the\s+portal\b|\bupdate\s+portal\s+knowledge\b',
    re.IGNORECASE,
)
_URL_DETECT_RE = re.compile(
    r'\b(?:https?://|www\.)\S+|\b\w[\w.-]+\.(?:com|org|net|io|in|co\.in)\b',
    re.IGNORECASE,
)
_ALIAS_QUESTION_RE = re.compile(
    r'\b(?:what|how|show|tell|check|find|get|is|when|where|who|which)\b',
    re.IGNORECASE,
)


def _derive_procedure_meta(user_input: str) -> tuple:
    """
    Returns (name, params_template, requires_variables) for a web query.

    Called when auto-saving a procedure after first successful execution.
    EOD queries get a variable placeholder for the daily task text.
    All other queries are saved as-is with no variable substitution.
    """
    if _EOD_RE.search(user_input):
        return (
            "Submit EOD",
            {"action": "query", "text": "submit my eod: {tasks_done}"},
            ["tasks_done"],
        )
    name = re.sub(r"\s+", " ", user_input.strip())[:40].title().rstrip("?")
    return (name, {"action": "query", "text": user_input}, [])


class TaskRouter:
    def __init__(self, llm: LLMEngine, narration: Narration, tool_registry=None):
        self.llm = llm
        self.narration = narration
        self.tools = tool_registry
        self._history: list[dict] = []  # conversation memory for current session

        # Phase 4: procedure memory — gracefully absent if modules not ready
        self._proc_svc = None
        self._matcher  = None
        self._runner   = None
        self._init_procedure_memory(tool_registry, narration)

    def _init_procedure_memory(self, tool_registry, narration) -> None:
        try:
            from memory.procedure_service import ProcedureService
            from memory.procedure_matcher import ProcedureMatcher
            from core.procedure_runner import ProcedureRunner
            self._proc_svc = ProcedureService()
            self._matcher  = ProcedureMatcher()
            self._runner   = ProcedureRunner(tool_registry, narration, self._proc_svc)
            logger.info("[ROUTER] Phase 4 procedure memory enabled")
        except Exception as e:
            logger.warning("[ROUTER] Phase 4 not available (JARVIS works without it): {}", e)

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
        # Discovery: "watch https://people.codeclouds.com/my-activity"
        m = _WATCH_RE.search(user_input)
        if m:
            url = m.group(1).strip()
            logger.info("[ROUTER] Watch/discovery command — url={!r}", url)
            return {"tool": "web", "params": {"action": "discover_site", "text": url}}

        # Phase 5.5 — Tag teaching: "when I say paipa, I mean my office portal"
        # Must run BEFORE resolver — query contains portal keywords that would pre-route it
        if _TAG_TEACH_RE.search(user_input):
            logger.info("[ROUTER] Tag teaching command — routing to web engine")
            return {"tool": "web", "params": {"action": "teach_tag", "text": user_input}}

        # Phase 5.5 — Manual portal knowledge refresh: "refresh portal knowledge"
        if _REFRESH_RE.search(user_input):
            logger.info("[ROUTER] Manual refresh command — routing to web engine")
            return {"tool": "web", "params": {"action": "refresh_knowledge", "text": user_input}}

        # Phase 5.5 — URL response: user replied to "Could you give me the URL?"
        # Must run BEFORE resolver so a bare domain isn't mis-routed as a portal query
        _web_engine = getattr(self.tools, '_tools', {}).get("web") if self.tools else None
        if _web_engine and getattr(_web_engine, '_pending_site_query', None) and _URL_DETECT_RE.search(user_input):
            logger.info("[ROUTER] URL response detected — routing to web engine (teach_site)")
            return {"tool": "web", "params": {"action": "teach_site", "text": user_input}}

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

        # Phase 5.5 — Alias response: user replied to "Any other names for it?"
        # Placed LAST so normal portal queries (resolver match) take priority.
        # Only catch short inputs that don't look like data queries.
        if _web_engine and getattr(_web_engine, '_pending_alias_site_id', None):
            if _ALIAS_QUESTION_RE.search(user_input):
                # Looks like a real question — user moved on; clear the alias state
                logger.debug("[ROUTER] Question word detected during alias wait — clearing alias state")
                _web_engine._pending_alias_site_id = None
                _web_engine._original_query_after_teach = None
            else:
                logger.info("[ROUTER] Alias response detected — routing to web engine (add_aliases)")
                return {"tool": "web", "params": {"action": "add_aliases", "text": user_input}}

        return None

    def _handle_provider_switch(self, user_input: str) -> str | None:
        """
        Detects 'switch to OpenAI' / 'switch to Ollama' voice commands.
        Returns spoken response if matched, None otherwise.
        """
        m = _SWITCH_RE.search(user_input)
        if not m:
            return None

        target = "openai" if "openai" in m.group(2).lower().replace(" ", "") else "ollama"

        try:
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            logger.error(f"[SWITCH] Cannot read config: {e}")
            return f"{personality.say('system_error')} couldn't read my config file."
        # ToDo - maybe we can add a personality module response here too, random, if needed add different personality category.

        current = config["llm"].get("provider", "ollama")
        if target == current:
            return f"{personality.say('system_confirm')} You're already on {target}."
        # ToDo - maybe we can add a personality module response here too, random, if needed add different personality category.

        if target == "openai" and not config["llm"].get("openai_api_key", "").strip():
            return (
                f"{personality.say('system_error')} no OpenAI API key configured. "
                "Please run 'python setup.py' in the terminal to add it."
            )
        # ToDo - maybe we can add a personality module response here too, random, if needed add different personality category.

        config["llm"]["provider"] = target
        try:
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4)
        except Exception as e:
            logger.error(f"[SWITCH] Cannot write config: {e}")
            return f"{personality.say('system_error')} couldn't save the config change."
        # ToDo - maybe we can add a personality module response here too, random, if needed add different personality category.

        self.llm.reload_provider(config)
        provider_label = "OpenAI" if target == "openai" else "Ollama"
        logger.info(f"[SWITCH] Switched to {provider_label}")
        return f"{personality.say('system_confirm')} Switched to {provider_label}. No restart needed."
        # ToDo - maybe we can add a personality module response here too, random, if needed add different personality category.

    # ──────────────────────────────────────────────────────────────────────────
    # Phase 4: Procedure execution
    # ──────────────────────────────────────────────────────────────────────────

    async def _try_procedure(self, user_input: str) -> str | None:
        """
        Check if user_input matches a saved procedure and run it.

        Returns a spoken response string if a procedure was matched (and run),
        or None to signal fall-through to normal routing.
        """
        if not self._matcher or not self._runner:
            return None

        loop = asyncio.get_event_loop()
        try:
            match = await loop.run_in_executor(None, self._matcher.find, user_input)
        except Exception as e:
            logger.warning("[ROUTER] Procedure match error (falling through): {}", e)
            return None

        if match is None:
            return None

        # Two close matches — ask user to clarify
        if match.get("ambiguous"):
            candidates = match["candidates"]
            names = " or ".join(f'"{c["name"]}"' for c in candidates[:2])
            response = f"I found two matching saved tasks: {names}. Which one did you mean, sir?"
            self._add_to_history("user", user_input)
            self._add_to_history("assistant", response)
            return response

        procedure = match
        logger.info("[ROUTER] Procedure match: '{}' id={}", procedure["name"], procedure["id"])

        # Double-submit guard (e.g. EOD already submitted 10s ago)
        if self._runner.would_double_submit(procedure):
            response = (
                f"I ran {procedure['name']} just a moment ago. "
                "Did you want to run it again, sir?"
            )
            self._add_to_history("user", user_input)
            self._add_to_history("assistant", response)
            return response

        # Extract variables from user speech if the procedure requires them
        requires_vars = procedure.get("requires_variables", [])
        resolved_vars = {}
        if requires_vars:
            try:
                resolved_vars = await self.llm.extract_variables(requires_vars, user_input)
                logger.info("[ROUTER] Extracted vars: {}", resolved_vars)
            except Exception as e:
                logger.warning("[ROUTER] Variable extraction failed (proceeding without): {}", e)

        # Run the procedure
        from core.procedure_runner import ProcedureExecutionError
        try:
            result = await loop.run_in_executor(
                None, self._runner.run, procedure, resolved_vars
            )
        except ProcedureExecutionError as e:
            logger.error("[ROUTER] Procedure '{}' failed: {}", procedure["name"], e)
            error_narr = procedure.get("narration_error", "")
            if not error_narr:
                self.narration.step("Something went wrong.")
            response = (
                f"I ran into a problem with {procedure['name']}. "
                "Let me try another way, sir."
            )
            self._add_to_history("user", user_input)
            self._add_to_history("assistant", response)
            return response

        # PASSTHROUGH: procedure broken — delete it so next query re-learns
        if str(result).startswith(_PASSTHROUGH_PREFIX):
            try:
                await loop.run_in_executor(None, self._proc_svc.delete, procedure["id"])
                logger.info("[ROUTER] Deleted broken procedure '{}' id={}",
                            procedure["name"], procedure["id"])
            except Exception as del_e:
                logger.warning("[ROUTER] Could not delete broken procedure: {}", del_e)
            response = str(result)[len(_PASSTHROUGH_PREFIX):]
            self._add_to_history("user", user_input)
            self._add_to_history("assistant", response)
            return response

        # Summarize the result for spoken output
        summary_prompt = (
            f"The user asked: {user_input}\n"
            f"Result: {result}\n"
            "In 1-2 spoken sentences (max 40 words), state the key finding. "
            "Quote exact numbers and percentages as given."
        )
        response = await self.llm.ask(
            summary_prompt,
            model=self.llm.fast_model,
            style={"num_predict": 100, "length_hint": "Summarise in 1-2 spoken sentences."},
        )
        self._add_to_history("user", user_input)
        self._add_to_history("assistant", response)
        return response

    # ──────────────────────────────────────────────────────────────────────────
    # Phase 4: Forget / correct commands
    # ──────────────────────────────────────────────────────────────────────────

    async def _try_procedure_management(self, user_input: str) -> str | None:
        """
        Handle 'forget X' and 'correct X' voice commands.

        Uses a keyword gate first so we don't call the LLM on every query.
        Returns a spoken response if a management command was detected, else None.
        """
        if not self._proc_svc:
            return None

        u_lower = user_input.lower()
        if not any(hint in u_lower for hint in _MGMT_HINTS):
            return None

        try:
            intent_result = await self.llm.detect_correction_intent(user_input)
            intent = intent_result.get("intent", "none")
        except Exception as e:
            logger.debug("[ROUTER] detect_correction_intent failed: {}", e)
            return None

        if intent == "forget":
            all_procs = self._proc_svc.list_all()
            target = None
            for p in all_procs:
                if p["name"].lower() in u_lower:
                    target = p
                    break

            if target:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._proc_svc.delete, target["id"])
                logger.info("[ROUTER] User deleted procedure '{}' id={}", target["name"], target["id"])
                response = f"Done. I've forgotten how to {target['name'].lower()}, sir."
            else:
                response = (
                    "I don't have a saved procedure with that name. "
                    "Which one did you mean, sir?"
                )
            self._add_to_history("user", user_input)
            self._add_to_history("assistant", response)
            return response

        if intent == "correct":
            response = "Understood, sir. Please show me the correct way and I'll learn it from scratch."
            self._add_to_history("user", user_input)
            self._add_to_history("assistant", response)
            return response

        return None

    # ──────────────────────────────────────────────────────────────────────────
    # Phase 4: Auto-save a procedure after first-time execution
    # ──────────────────────────────────────────────────────────────────────────

    async def _auto_save_procedure(self, user_input: str, recorder) -> None:
        """
        After a web tool execution that was recorded, save the steps as a procedure
        so future identical queries can be replayed without LLM routing.

        Only saves if:
        - recorder captured at least one step (Playwright/form path — not API/cache)
        - no procedure with the same name already exists
        """
        if not self._proc_svc or recorder.step_count() == 0:
            return

        name, params_template, requires_variables = _derive_procedure_meta(user_input)

        # Don't save a duplicate
        existing = self._proc_svc.find_by_name(name)
        if existing:
            logger.debug("[ROUTER] Procedure '{}' already saved — skipping", name)
            return

        steps = recorder.commit()

        description = f"Handles the request: {user_input[:80]}"
        try:
            triggers = await self.llm.generate_procedure_triggers(name, description)
        except Exception as e:
            logger.warning("[ROUTER] Trigger generation failed (using fallback): {}", e)
            triggers = [user_input.lower().strip()]

        narration_cfg = {
            "start": f"Running {name}...",
            "done":  "Done.",
            "error": "Something went wrong.",
        }
        try:
            proc_id = self._proc_svc.save(
                name=name,
                tool="web",
                params_template=params_template,
                triggers=triggers,
                steps=steps,
                requires_variables=requires_variables,
                narration=narration_cfg,
            )
            logger.info("[ROUTER] Saved procedure '{}' id={} triggers={}", name, proc_id, len(triggers))
            self.narration.step("I've learned how to do that. I'll remember it for next time, sir.")
        except Exception as e:
            logger.error("[ROUTER] Failed to save procedure '{}': {}", name, e)

    # ──────────────────────────────────────────────────────────────────────────
    # Main dispatch
    # ──────────────────────────────────────────────────────────────────────────

    async def handle(self, user_input: str) -> str:
        if not user_input.strip():
            return personality.say("didnt_catch")
        # Todo - can we use the personality module? personality.say("didnt_catch")? 

        # Check for provider switch command before anything else
        switch_response = self._handle_provider_switch(user_input)
        if switch_response is not None:
            self._add_to_history("user", user_input)
            self._add_to_history("assistant", switch_response)
            return switch_response

        logger.info(f"[ROUTER] ── New request ──────────────────────")
        logger.info(f"[ROUTER] User said: {user_input!r}")
        logger.info(f"[ROUTER] History: {len(self._history)} messages in context")

        # Phase 4: procedure match FIRST — before keyword routing and LLM
        proc_response = await self._try_procedure(user_input)
        if proc_response is not None:
            return proc_response

        # Phase 4: forget / correct commands
        mgmt_response = await self._try_procedure_management(user_input)
        if mgmt_response is not None:
            return mgmt_response

        # Step 1: Route decision — fast pre-router first, then LLM fallback
        t_route = time.perf_counter()
        decision = self._pre_route(user_input)
        if decision is None:
            decision = await self.llm.route(user_input, history=self._history)
        logger.info(f"[ROUTER] Route took {time.perf_counter() - t_route:.1f}s → {decision}")

        tool_name = decision.get("tool", "answer")

        if tool_name in ("llm_offline", "ollama_offline"):
            provider = self.llm._provider
            hint = "make sure Ollama is running" if provider == "ollama" else "check your OpenAI API key"
            return f"I can't reach my language model right now. Please {hint}, sir."

        # Step 2a: Tool path
        if tool_name != "answer" and self.tools:
            params = decision.get("params", {})
            # Always use the original user text for the web tool — LLM paraphrases break tag matching
            if tool_name == "web":
                params["text"] = user_input

            # Phase 4: inject recorder into web calls so steps can be learned
            _recorder = None
            if tool_name == "web" and self._proc_svc:
                try:
                    from memory.procedure_recorder import ProcedureRecorder
                    _recorder = ProcedureRecorder()
                    name_hint = _derive_procedure_meta(user_input)[0]
                    _recorder.begin(name_hint=name_hint)
                    params["_recorder"] = _recorder
                except Exception as e:
                    logger.debug("[ROUTER] Could not create recorder: {}", e)

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

            # Phase 4: auto-save procedure from recorder steps (non-blocking best-effort)
            if _recorder:
                try:
                    await self._auto_save_procedure(user_input, _recorder)
                except Exception as e:
                    logger.warning("[ROUTER] Auto-save procedure failed (non-fatal): {}", e)

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
