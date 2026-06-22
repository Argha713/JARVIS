import json
import re
import time
from datetime import date
from loguru import logger

CLASSIFY_PROMPT = """Classify this request as SIMPLE or COMPLEX.
SIMPLE: factual questions, short answers, yes/no, basic lookups.
COMPLEX: writing tasks, analysis, summarization of long text, comparisons.
Return one word only: SIMPLE or COMPLEX."""

ROUTE_PROMPT = """You decide how to handle the user's request.
Available tools:
- file_ops: search, explore, read, copy, or move files on this PC
  search:  {"action":"search","query":"<name or type>"}
  explore: {"action":"explore","query":"<project or folder name>"}
  read:    {"action":"read","path":"<absolute path>"}
  copy:    {"action":"copy","src":"<src>","dst":"<dst>"}
  move:    {"action":"move","src":"<src>","dst":"<dst>"}

- browser_extension: search Google or interact with the web via the user's real browser
  search:  {"action":"search","query":"<search query>"}

- browser: open a specific URL and read its contents
  read:    {"action":"read","url":"<full url>"}

- web: find or interact with data on a website, portal, HR system, or online dashboard
  Use when the user asks about their office portal, HR data, attendance, leave, EOD,
  seat booking, or any personal data that lives on a website.
  Always pass the user's exact words: {"action":"query","text":"<exact user request>"}

Respond with a SINGLE JSON object only — no explanation, no markdown, no arrays.
Use a tool: {"tool":"<tool_name>","params":{...}}
Answer directly: {"tool":"answer"}

Examples:
"find my resume" → {"tool":"file_ops","params":{"action":"search","query":"resume"}}
"what is the leadmacro project" → {"tool":"file_ops","params":{"action":"explore","query":"leadmacro"}}
"what is the weather in Kolkata" → {"tool":"browser_extension","params":{"action":"search","query":"weather in Kolkata"}}
"latest news about AI" → {"tool":"browser_extension","params":{"action":"search","query":"latest AI news"}}
"open bbc.com and tell me the top story" → {"tool":"browser","params":{"action":"read","url":"https://www.bbc.com"}}
"what is my attendance this month" → {"tool":"web","params":{"action":"query","text":"what is my attendance this month"}}
"how many casual leaves do I have left" → {"tool":"web","params":{"action":"query","text":"how many casual leaves do I have left"}}
"submit my EOD: fixed login bug and reviewed PRs" → {"tool":"web","params":{"action":"query","text":"submit my EOD: fixed login bug and reviewed PRs"}}
"what is my punctuality rate" → {"tool":"web","params":{"action":"query","text":"what is my punctuality rate"}}
"what is the capital of France" → {"tool":"answer"}
"from which website did you get that" → {"tool":"answer"}"""

KNOWN_TOOLS = {"file_ops", "browser_extension", "browser", "web", "answer"}
_KEYWORDS_PATH = "data/style_keywords.json"


def _load_style_keywords() -> dict:
    try:
        with open(_KEYWORDS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        logger.debug(f"Style keywords loaded from {_KEYWORDS_PATH}")
        return data
    except FileNotFoundError:
        logger.warning(f"Style keywords file not found at {_KEYWORDS_PATH} — using defaults")
        return {
            "explain": ["explain", "in detail", "how does", "why does", "describe"],
            "chat":    ["what do you think", "how are you", "hello", "hey jarvis"],
            "quick":   ["what is ", "who is ", "when did ", "how many"],
        }


# Loaded once at startup — edit data/style_keywords.json and restart to apply
_STYLE_KEYWORDS = _load_style_keywords()


def _detect_style(text: str) -> dict:
    t = text.lower()
    if any(k in t for k in _STYLE_KEYWORDS.get("explain", [])):
        return {"num_predict": 500, "length_hint": "Explain thoroughly. You may use up to 150 words."}
    if any(k in t for k in _STYLE_KEYWORDS.get("chat", [])):
        return {"num_predict": 200, "length_hint": "Respond naturally and warmly, like a close colleague."}
    if any(t.startswith(k) or f" {k}" in t for k in _STYLE_KEYWORDS.get("quick", [])):
        return {"num_predict": 80,  "length_hint": "Answer in one sentence only."}
    return      {"num_predict": 120, "length_hint": "Keep response under 40 words."}


def _routing_system_prompt(length_hint: str = "Keep response under 40 words.") -> str:
    today = date.today().strftime("%A, %B %d, %Y")
    return (
        f"You are JARVIS, a personal AI assistant.\n"
        f"Today's date is {today}.\n"
        "You are speaking aloud — no markdown, no bullet points, no numbered lists.\n"
        f"{length_hint}\n"
        "Speak naturally, as if in conversation."
    )


class LLMEngine:
    def __init__(self, config: dict):
        self._init_provider(config)

    def _init_provider(self, config: dict) -> None:
        llm_cfg = config["llm"]
        self._provider = llm_cfg.get("provider", "ollama")

        if self._provider == "openai":
            import openai
            self._client = openai.AsyncOpenAI(api_key=llm_cfg.get("openai_api_key", ""))
            self.fast_model  = llm_cfg.get("openai_fast_model", "gpt-4o-mini")
            self.smart_model = llm_cfg.get("openai_smart_model", "gpt-4o")
        else:
            import ollama
            self._client = ollama.AsyncClient(host=llm_cfg.get("ollama_host", "http://localhost:11434"))
            self.fast_model  = llm_cfg.get("fast_model", "phi3")
            self.smart_model = llm_cfg.get("smart_model", "llama3.1:8b")

        logger.info(f"[LLM] Provider: {self._provider} | fast={self.fast_model} | smart={self.smart_model}")

    def reload_provider(self, config: dict) -> None:
        """Hot-swap provider without restarting JARVIS."""
        self._init_provider(config)

    async def _chat(self, model: str, messages: list, num_predict: int) -> str:
        """Single call point for both providers. Raises on connection failure."""
        if self._provider == "openai":
            response = await self._client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=num_predict,
            )
            return response.choices[0].message.content.strip()
        else:
            response = await self._client.chat(
                model=model,
                messages=messages,
                options={"num_predict": num_predict},
            )
            return response["message"]["content"].strip()

    async def classify(self, text: str) -> str:
        try:
            result = await self._chat(
                self.fast_model,
                [
                    {"role": "system", "content": CLASSIFY_PROMPT},
                    {"role": "user",   "content": text},
                ],
                num_predict=10,
            )
            return "COMPLEX" if "COMPLEX" in result.upper() else "SIMPLE"
        except Exception as e:
            logger.warning(f"[CLASSIFY] Error: {e} — defaulting to SIMPLE")
            return "SIMPLE"

    async def route(self, text: str, history: list = None) -> dict:
        logger.info(f"[ROUTE] Input: {text!r}")
        t0 = time.perf_counter()

        # Include the last 2 conversation turns so the router can resolve
        # follow-ups like "What about in Kolkata?" using context ("weather" from
        # the previous exchange) without misrouting to file_ops or news.
        messages = [{"role": "system", "content": ROUTE_PROMPT}]
        if history:
            for msg in history[-4:]:   # last 4 entries = 2 turns (user + assistant each)
                messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": text})

        logger.debug("[ROUTE] Full request ({} messages):", len(messages))
        for i, m in enumerate(messages):
            logger.debug("[ROUTE]   [{}] role={} | {!r}", i, m["role"], m["content"][:300])

        try:
            raw = await self._chat(
                self.fast_model,
                messages,
                num_predict=100,
            )
        except Exception as e:
            logger.error(f"[ROUTE] LLM unreachable: {e}")
            return {"tool": "llm_offline"}

        elapsed = time.perf_counter() - t0
        logger.info(f"[ROUTE] LLM raw ({elapsed:.1f}s): {raw!r}")

        try:
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                if "```" in raw:
                    raw = raw.split("```")[1].lstrip("json").strip()
                start = raw.find('{')
                end   = raw.rfind('}')
                if start != -1 and end != -1 and end > start:
                    raw = raw[start:end + 1]
                result = json.loads(raw)

            # phi3 sometimes returns {"tool_sequence": [...]}
            if "tool_sequence" in result:
                seq = result.get("tool_sequence", [])
                result = seq[0] if seq else {"tool": "answer"}
                logger.info(f"[ROUTE] Extracted first from tool_sequence: {result}")

            # phi3 sometimes uses "action" as top-level key
            if "tool" not in result and "action" in result:
                action_val = result.get("action", "")
                if action_val in KNOWN_TOOLS:
                    result["tool"] = result.pop("action")
                    logger.info(f"[ROUTE] Normalized top-level 'action' → 'tool': {result['tool']}")

            if "tool" not in result:
                logger.warning(f"[ROUTE] Missing 'tool' key in {result} — fallback to answer")
                return {"tool": "answer"}

            if result["tool"] not in KNOWN_TOOLS:
                logger.warning(f"[ROUTE] Unknown tool '{result['tool']}' — fallback to answer")
                return {"tool": "answer"}

            logger.info(f"[ROUTE] Decision: tool={result['tool']} params={result.get('params', {})}")
            return result

        except Exception as e:
            logger.warning(f"[ROUTE] Parse failed ({e}) — fallback to answer. Raw: {raw!r}")
            return {"tool": "answer"}

    async def extract_variables(self, requires_variables: list, user_input: str) -> dict:
        """
        Extract values for required procedure variables from the user's speech.

        requires_variables: ["tasks_done", "date"]
        user_input: "submit my eod: fixed login bug and reviewed PRs"

        Returns: {"tasks_done": "fixed login bug and reviewed PRs"}
        Missing or ambiguous variables are omitted from the result — the caller
        must handle and ask the user to clarify.
        """
        if not requires_variables:
            return {}

        var_list = ", ".join(f'"{v}"' for v in requires_variables)
        prompt = (
            f"Extract the following values from the user's input. "
            f"Variables to extract: [{var_list}].\n"
            f"User said: \"{user_input}\"\n\n"
            f"Return a JSON object mapping each variable name to its extracted value. "
            f"Omit any variable you cannot confidently extract. "
            f"Return the raw JSON object only — no explanation, no markdown."
        )
        try:
            raw = await self._chat(
                self.fast_model,
                [{"role": "user", "content": prompt}],
                num_predict=200,
            )
            raw = raw.strip()
            if "```" in raw:
                raw = raw.split("```")[1].lstrip("json").strip().rstrip("```").strip()
            start, end = raw.find("{"), raw.rfind("}")
            if start != -1 and end != -1:
                raw = raw[start:end + 1]
            result = json.loads(raw)
            return {k: str(v) for k, v in result.items() if k in requires_variables}
        except Exception as e:
            logger.warning("[LLM] extract_variables failed: {}", e)
            return {}

    async def generate_procedure_triggers(self, procedure_name: str, description: str) -> list:
        """
        Generate 5–8 natural language trigger phrases for a procedure.
        These are stored in ChromaDB and matched semantically against future voice input.

        Returns: ["submit my eod", "send my work report", "log daily tasks", ...]
        Falls back to [procedure_name.lower()] on failure.
        """
        prompt = (
            f"Generate 5 to 8 short, natural language voice command phrases that a user might "
            f"say to trigger the following task.\n"
            f"Task name: \"{procedure_name}\"\n"
            f"Description: \"{description}\"\n\n"
            f"Rules:\n"
            f"- Each phrase should be what a person would actually say aloud\n"
            f"- Vary phrasing (synonyms, different word orders)\n"
            f"- All lowercase, no punctuation\n"
            f"- Return a JSON array of strings only — no explanation, no markdown"
        )
        try:
            raw = await self._chat(
                self.fast_model,
                [{"role": "user", "content": prompt}],
                num_predict=200,
            )
            raw = raw.strip()
            if "```" in raw:
                raw = raw.split("```")[1].lstrip("json").strip().rstrip("```").strip()
            start, end = raw.find("["), raw.rfind("]")
            if start != -1 and end != -1:
                raw = raw[start:end + 1]
            phrases = json.loads(raw)
            if isinstance(phrases, list) and all(isinstance(p, str) for p in phrases):
                return [p.strip().lower() for p in phrases if p.strip()]
        except Exception as e:
            logger.warning("[LLM] generate_procedure_triggers failed: {}", e)
        return [procedure_name.lower()]

    async def detect_correction_intent(self, user_input: str) -> dict:
        """
        Classify whether the user is:
        - correcting a procedure  ("use a different site next time", "that's wrong, redo it")
        - asking to forget/delete a procedure  ("forget how to submit eod", "delete that procedure")
        - neither  (normal request)

        Returns: {"intent": "correct"} | {"intent": "forget"} | {"intent": "none"}
        """
        prompt = (
            f"Classify the user's intent. They may be:\n"
            f"  \"correct\" — correcting how JARVIS performed a saved task "
            f"(e.g. \"use accuweather next time\", \"that was wrong\", \"redo it differently\")\n"
            f"  \"forget\"  — asking JARVIS to forget/delete a saved procedure "
            f"(e.g. \"forget how to submit eod\", \"delete that task\", \"stop remembering that\")\n"
            f"  \"none\"    — neither of the above\n\n"
            f"User said: \"{user_input}\"\n\n"
            f"Return exactly one JSON object: {{\"intent\": \"correct\"}} or "
            f"{{\"intent\": \"forget\"}} or {{\"intent\": \"none\"}}. "
            f"No explanation, no markdown."
        )
        try:
            raw = await self._chat(
                self.fast_model,
                [{"role": "user", "content": prompt}],
                num_predict=20,
            )
            raw = raw.strip()
            start, end = raw.find("{"), raw.rfind("}")
            if start != -1 and end != -1:
                raw = raw[start:end + 1]
            result = json.loads(raw)
            intent = result.get("intent", "none")
            if intent not in ("correct", "forget", "none"):
                intent = "none"
            return {"intent": intent}
        except Exception as e:
            logger.warning("[LLM] detect_correction_intent failed: {}", e)
            return {"intent": "none"}

    async def ask(self, prompt: str, model: str = None, history: list = None,
                  style: dict = None) -> str:
        if model is None:
            complexity = await self.classify(prompt)
            model = self.smart_model if complexity == "COMPLEX" else self.fast_model

        if style is None:
            style = _detect_style(prompt)

        num_predict = style["num_predict"]
        length_hint = style["length_hint"]

        messages = [{"role": "system", "content": _routing_system_prompt(length_hint)}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": prompt})

        logger.info(
            f"[LLM] Provider: {self._provider} | Model: {model} | "
            f"Style: num_predict={num_predict} | History: {len(history) if history else 0} turns"
        )
        logger.debug("[LLM] Full request ({} messages):", len(messages))
        for i, m in enumerate(messages):
            logger.debug("[LLM]   [{}] role={} | {!r}", i, m["role"], m["content"][:400])

        t0 = time.perf_counter()

        try:
            answer = await self._chat(model, messages, num_predict)
        except Exception as e:
            logger.error(f"[LLM] Error: {e}")
            return "I can't reach my language model right now. Please check your provider settings, sir."

        elapsed = time.perf_counter() - t0
        logger.info(f"[LLM] Done in {elapsed:.1f}s | Response ({len(answer)} chars):\n{answer}")
        logger.debug("[LLM] Full response:\n{}", answer)
        return answer
