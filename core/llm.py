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

- web_search: search the internet for current information, news, weather, prices
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
"what is the weather in Kolkata" → {"tool":"web_search","params":{"action":"search","query":"weather in Kolkata"}}
"latest news about AI" → {"tool":"web_search","params":{"action":"search","query":"latest AI news"}}
"open bbc.com and tell me the top story" → {"tool":"browser","params":{"action":"read","url":"https://www.bbc.com"}}
"what is my attendance this month" → {"tool":"web","params":{"action":"query","text":"what is my attendance this month"}}
"how many casual leaves do I have left" → {"tool":"web","params":{"action":"query","text":"how many casual leaves do I have left"}}
"submit my EOD: fixed login bug and reviewed PRs" → {"tool":"web","params":{"action":"query","text":"submit my EOD: fixed login bug and reviewed PRs"}}
"what is my punctuality rate" → {"tool":"web","params":{"action":"query","text":"what is my punctuality rate"}}
"what is the capital of France" → {"tool":"answer"}
"from which website did you get that" → {"tool":"answer"}"""

KNOWN_TOOLS = {"file_ops", "web_search", "browser", "web", "answer"}
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
