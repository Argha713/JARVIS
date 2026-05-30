"""
Resolver: extracts site + intent from a raw user query.

Site resolution order:
  1. Exact tag match
  2. Fuzzy phrase match (rapidfuzz partial_ratio ≥ 80)
  3. Word-level fuzzy match (catches STT garbling, e.g. "punctual" ≈ "punctuality")
  4. Unknown → returns None (caller asks user for URL)

Intent: "read" or "write"
  write keywords: submit, send, book, fill, create, update, delete, add, post
"""
import re
from typing import Optional

from loguru import logger
from rapidfuzz import fuzz, process

from tools.web_engine import store

_WRITE_KEYWORDS = {
    "submit", "send", "book", "fill", "create", "update",
    "delete", "add", "post", "write", "record", "log",
}

# Minimum fuzzy score to accept a tag match (0-100)
_FUZZY_THRESHOLD = 80


def resolve(query: str) -> tuple[Optional[str], str]:
    """
    Returns (site_id, intent) where intent is "read" or "write".
    site_id is None if the site is unknown.
    """
    intent = _detect_intent(query)
    site_id = _resolve_site(query)
    logger.debug("[RESOLVER] query={!r} → site={!r} intent={!r}", query[:60], site_id, intent)
    return site_id, intent


def _detect_intent(query: str) -> str:
    words = set(re.findall(r"\w+", query.lower()))
    if words & _WRITE_KEYWORDS:
        return "write"
    return "read"


def _resolve_site(query: str) -> Optional[str]:
    all_tags = store.all_tags()   # [(site_id, tag), ...]
    if not all_tags:
        return None

    query_lower = query.lower()

    # 1. Exact substring match
    for site_id, tag in all_tags:
        if tag in query_lower:
            logger.debug("[RESOLVER] Exact tag match: {!r} → {}", tag, site_id)
            return site_id

    # 2. Fuzzy match — score each tag against the full query
    tag_strings = [tag for _, tag in all_tags]
    match = process.extractOne(
        query_lower,
        tag_strings,
        scorer=fuzz.partial_ratio,
        score_cutoff=_FUZZY_THRESHOLD,
    )
    if match:
        matched_tag, score, idx = match
        site_id = all_tags[idx][0]
        logger.debug("[RESOLVER] Fuzzy tag match: {!r} (score={}) → {}",
                     matched_tag, score, site_id)
        return site_id

    # 3. Word-level fuzzy match — catches STT garbling like "punctual" ≈ "punctuality"
    #    Each word in the query is compared against each word in each tag.
    #    Only considers words ≥ 5 chars to avoid noisy short-word matches.
    q_words = [w for w in re.findall(r'\w+', query_lower) if len(w) >= 5]
    for q_word in q_words:
        for site_id, tag in all_tags:
            for t_word in re.findall(r'\w+', tag):
                if len(t_word) >= 5 and fuzz.ratio(q_word, t_word) >= 82:
                    logger.debug("[RESOLVER] Word-level fuzzy: {!r} ~ {!r} → {}",
                                 q_word, t_word, site_id)
                    return site_id

    return None


def register_site(site_id: str, base_url: str, name: str, tags: list[str]) -> None:
    """Persist a newly learned site with its initial tags."""
    store.upsert_site(site_id, base_url, name)
    for tag in tags:
        store.add_tag(site_id, tag)
    logger.info("[RESOLVER] Site registered: {} | tags: {}", site_id, tags)


def add_tag_for_site(query: str, site_id: str) -> None:
    """
    Parse 'when I say X, I mean Y' style commands.
    Also used to directly add a tag when a new alias is learned.
    """
    # Pattern: "when I say X, I mean <site reference>"
    m = re.search(r"when\s+i\s+say\s+['\"]?(.+?)['\"]?,?\s+i\s+mean", query, re.I)
    if m:
        new_tag = m.group(1).strip().lower()
        store.add_tag(site_id, new_tag)
        logger.info("[RESOLVER] New alias {!r} added for {}", new_tag, site_id)
        return

    # Direct: caller passes the alias explicitly
    store.add_tag(site_id, query.strip().lower())


def extract_tags_from_query(query: str) -> list[str]:
    """
    Extract possible site reference words from a query for auto-tagging
    after a user provides a URL.
    Strips common stop words so "what is my punctuality on my hr portal"
    returns ["hr portal"].
    """
    _STOP = {
        "what", "is", "my", "the", "a", "an", "on", "in", "at", "to",
        "i", "me", "for", "of", "and", "or", "how", "many", "check",
        "find", "get", "tell", "show", "open", "please",
    }
    words = query.lower().split()
    # Build candidate: last 2-3 non-stop words before end of query
    meaningful = [w.strip("?.,!") for w in words if w not in _STOP and len(w) > 2]
    if not meaningful:
        return []
    # Return the last 3 as a phrase (most likely to be the site reference)
    phrase = " ".join(meaningful[-3:])
    return [phrase]
