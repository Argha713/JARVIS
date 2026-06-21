import asyncio
import json
import random
import re
from datetime import datetime, timedelta
from loguru import logger

from tools.web_engine import store

_CATEGORIES = ["listening", "timeout", "didnt_catch", "acknowledge", "nonsense", "error", "system_error", "system_confirm"]

_CATEGORY_CONTEXT = {
    "listening":       "played when JARVIS starts listening for the user's command",
    "timeout":         "played when the user went silent and JARVIS returns to standby",
    "didnt_catch":     "played when audio was too noisy or unclear to understand",
    "acknowledge":     "quick acknowledgment before JARVIS processes a command (1-5 words max)",
    "nonsense":        "played when the user is clearly playing around or said gibberish",
    "error":           "played when a technical error occurs internally",
    "system_error":    "very short prefix before a specific error detail — ends with a dash so the fact follows naturally (e.g. 'No luck — couldn\\'t read config')",
    "system_confirm":  "confirms a completed action or current system state — short and punchy, works for both 'done' and 'already set' situations",
}

_SEEDS = {
    "listening":      ["Listening.", "Go ahead, sir.", "I'm all ears."],
    "timeout":        ["Standing by.", "I'll be here.", "Silence noted. Whenever you're ready."],
    "didnt_catch":    ["I didn't catch that.", "Say that again?", "Come again?"],
    "acknowledge":    ["On it.", "Got it.", "Right away."],
    "nonsense":       ["That was... something.", "Are you speaking human today?", "Fascinating input."],
    "error":          ["Something went wrong on my end.", "Hit a snag. Try again.", "That didn't go as planned."],
    "system_error":   ["Ran into a snag —", "No luck —", "Hit a wall —", "Something's off —", "Uh oh —"],
    "system_confirm": ["Done.", "All set, sir.", "Sorted.", "Noted.", "Consider it done."],
}

_WHISPER_SEED = {
    "thanks for watching", "thank you for watching", "thank you", "thanks",
    "you", ".", "..", "...", "subtitles by", "subscribe", "bye",
    "please subscribe", "like and subscribe",
}

_PERSONALITY_PROMPT = (
    "You are JARVIS — a witty, intelligent AI assistant and the user's buddy.\n"
    "You're friendly, sometimes sarcastic, crack jokes, drop dry one-liners. Think Tony Stark's JARVIS.\n"
    "Never stiff or corporate. Occasional 'sir' is fine but don't overdo it.\n\n"
    "Generate exactly 15 fresh short phrases for the '{category}' response category.\n"
    "Context: these are {context}.\n\n"
    "Current phrases (do not repeat):\n{existing}\n\n"
    "Rules:\n"
    "- Each phrase must be under 12 words\n"
    "- Mix tones: some warm/friendly, some sarcastic, some funny\n"
    "- No emojis, no markdown, no numbering\n"
    "- Return ONLY a JSON array of 15 strings, nothing else"
)

# In-memory phrase pool — populated by boot(), hot-reloaded after each LLM refresh
_cache: dict = {}


def boot(config: dict) -> None:
    """Seed DB, load all phrases into memory. Sync, call once at startup."""
    store.init_db()
    _seed_db()
    for cat in _CATEGORIES:
        phrases = store.get_personality_phrases(cat)
        _cache[cat] = phrases if phrases else list(_SEEDS[cat])
    total = sum(len(v) for v in _cache.values())
    logger.info(f"[Personality] Loaded {total} phrases across {len(_CATEGORIES)} categories.")


def say(category: str) -> str:
    """Return a random phrase for the given category."""
    pool = _cache.get(category) or list(_SEEDS.get(category, ["..."]))
    return random.choice(pool)


async def refresh_loop(llm, config: dict) -> None:
    """Background coroutine: check staleness at startup then every check_interval_hours."""
    threshold_days = config.get("personality", {}).get("refresh_threshold_days", 20)
    interval_hours = config.get("personality", {}).get("check_interval_hours", 6)
    while True:
        await _check_all(llm, threshold_days)
        await asyncio.sleep(interval_hours * 3600)


async def _check_all(llm, threshold_days: int) -> None:
    cutoff = datetime.utcnow() - timedelta(days=threshold_days)
    for cat in _CATEGORIES:
        raw = store.get_personality_last_refreshed(cat)
        if raw is None or datetime.fromisoformat(raw) < cutoff:
            logger.info(f"[Personality] '{cat}' is stale — refreshing via LLM.")
            await _refresh_category(llm, cat)


async def _refresh_category(llm, category: str) -> None:
    existing = store.get_personality_phrases(category)
    prompt = _PERSONALITY_PROMPT.format(
        category=category,
        context=_CATEGORY_CONTEXT[category],
        existing=json.dumps(existing),
    )
    try:
        raw = await llm._chat(
            llm.fast_model,
            [{"role": "user", "content": prompt}],
            num_predict=800,
        )
        phrases = _parse_phrases(raw)
        if len(phrases) < 5:
            logger.warning(f"[Personality] LLM returned too few phrases for '{category}' — skipping.")
            return
        store.replace_llm_personality_phrases(category, phrases)
        store.mark_personality_refreshed(category)
        _cache[category] = store.get_personality_phrases(category)
        logger.info(f"[Personality] Refreshed '{category}' with {len(phrases)} new phrases.")
    except Exception as e:
        logger.warning(f"[Personality] Refresh failed for '{category}': {e} — keeping existing phrases.")


def _parse_phrases(raw: str) -> list:
    raw = raw.strip()
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(p).strip() for p in data if str(p).strip()]
    except json.JSONDecodeError:
        pass
    lines = raw.splitlines()
    return [re.sub(r"^[\d\-\*\.\s]+", "", l).strip() for l in lines if l.strip()]


def _seed_db() -> None:
    for cat, phrases in _SEEDS.items():
        for p in phrases:
            store.seed_personality_phrase(cat, p)
    for phrase in _WHISPER_SEED:
        store.seed_hallucination(phrase)
