import re


def detect_fresh_needed(query: str) -> bool:
    """
    Returns True if the query asks for a specific time period (e.g. 'last month',
    'in March') that can't be served from a generic cached result.
    """
    from tools.web_engine.discoverer import _extract_month_target
    return _extract_month_target(query) is not None


def extract_eod_text(query: str) -> str | None:
    """
    If the query is an EOD submission, return the text the user wants to submit.
    Returns None if the query isn't an EOD command.
    """
    m = re.search(
        r"(?:submit|send|log|write|record)\s+(?:my\s+)?(?:eod|end[- ]of[- ]day|work journal)[:\s]+(.+)",
        query, re.I,
    )
    return m.group(1).strip() if m else None


def detect_write_sub_intent(query: str) -> str:
    """Returns 'eod' or 'generic' for write-intent queries."""
    return "eod" if extract_eod_text(query) is not None else "generic"
