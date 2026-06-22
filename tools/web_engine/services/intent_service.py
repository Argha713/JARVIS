import re

from loguru import logger


def detect_fresh_needed(query: str) -> bool:
    """
    Returns True if the query asks for a specific time period (e.g. 'last month',
    'in March') that can't be served from a generic cached result.
    """
    from tools.web_engine.discoverer import _extract_month_target
    month_target = _extract_month_target(query)
    result       = month_target is not None
    if result:
        logger.debug("[IntentService] detect_fresh_needed: query={!r} → True (month={})", query[:60], month_target)
    else:
        logger.debug("[IntentService] detect_fresh_needed: query={!r} → False (no time-specific keyword)", query[:60])
    return result


def extract_eod_text(query: str) -> str | None:
    """
    If the query is an EOD submission, return the text the user wants to submit.
    Returns None if the query isn't an EOD command.
    """
    m = re.search(
        r"(?:submit|send|log|write|record)\s+(?:my\s+)?(?:eod|end[- ]of[- ]day|work journal)[:\s]+(.+)",
        query, re.I,
    )
    if m:
        text = m.group(1).strip()
        logger.debug("[IntentService] extract_eod_text: MATCH — eod_text={!r}", text[:80])
        return text
    logger.debug("[IntentService] extract_eod_text: query={!r} — no EOD pattern found", query[:60])
    return None


def detect_write_sub_intent(query: str) -> str:
    """Returns 'eod' or 'generic' for write-intent queries."""
    sub = "eod" if extract_eod_text(query) is not None else "generic"
    logger.debug("[IntentService] detect_write_sub_intent: query={!r} → sub_intent={!r}", query[:60], sub)
    return sub
