import re
from typing import Callable

from loguru import logger

from tools.web_engine import store, resolver

_PASSTHROUGH = "__PASSTHROUGH__:"

_NO_ALIAS = frozenset([
    "no", "nothing", "none", "nope", "skip", "done",
    "that's fine", "that's ok", "no thanks", "nevermind",
])


class SiteService:
    """
    Manages the multi-turn site onboarding state machine.
    Extracts all site teaching / alias handling out of WebEngine.
    """

    def __init__(self) -> None:
        self._pending_site_query: str | None = None       # set while waiting for user URL
        self._pending_alias_site_id: str | None = None    # set while waiting for aliases
        self._original_query_after_teach: str | None = None

    # ── State checks (used by engine to decide routing) ──────────────────

    @property
    def awaiting_site_url(self) -> bool:
        return self._pending_site_query is not None

    @property
    def awaiting_aliases(self) -> bool:
        return self._pending_alias_site_id is not None

    # ── Teach tag (anytime command) ───────────────────────────────────────

    def handle_teach_tag(self, user_input: str) -> str:
        """Handle 'when I say X, I mean Y' — adds a tag alias for a known site."""
        m = re.search(
            r"when\s+i\s+say\s+['\"]?(.+?)['\"]?,?\s+i\s+mean\s+(.+)",
            user_input, re.I,
        )
        if not m:
            return (
                f"{_PASSTHROUGH}I didn't quite understand that, sir. "
                f"Try: 'when I say X, I mean Y'."
            )
        new_alias = m.group(1).strip().lower()
        site_ref  = m.group(2).strip()
        site_id, _ = resolver.resolve(site_ref)
        if site_id:
            store.add_tag(site_id, new_alias)
            logger.info("[SiteService] Tag taught: {!r} → {}", new_alias, site_id)
            return f"{_PASSTHROUGH}Got it, sir. I'll recognise '{new_alias}' as {site_id} from now on."
        return (
            f"{_PASSTHROUGH}I don't know '{site_ref}' yet, sir. "
            f"Tell me its URL first and I'll remember the alias."
        )

    # ── Unknown site → ask user ───────────────────────────────────────────

    def ask_for_site(self, query: str) -> str:
        """Called when resolver returns None. Sets pending state and asks user for URL."""
        auto_tags = resolver.extract_tags_from_query(query)
        tag_hint  = f" ({', '.join(auto_tags)})" if auto_tags else ""
        self._pending_site_query        = query
        self._original_query_after_teach = query
        logger.info("[SiteService] Unknown site for query {!r} — asking user", query)
        return (
            f"{_PASSTHROUGH}I don't know which website{tag_hint} has that information. "
            f"Could you give me the URL, sir?"
        )

    # ── User provides URL ─────────────────────────────────────────────────

    def handle_teach_site(self, url_input: str) -> str:
        """Handle user's URL reply. Registers the site and asks for aliases."""
        original_query          = self._pending_site_query
        self._pending_site_query = None

        url_m = re.search(
            r'(https?://\S+|(?:www\.)?[\w][\w.-]+\.(?:com|org|net|io|in|co\.in)(?:/\S*)?)',
            url_input, re.I,
        )
        if not url_m:
            self._pending_site_query = original_query  # let user retry
            return f"{_PASSTHROUGH}I couldn't find a URL in that. Could you say it again, sir?"

        raw_url   = url_m.group(1)
        auto_tags = resolver.extract_tags_from_query(original_query or url_input)
        response  = self.teach_site(raw_url, auto_tags)

        base_url = raw_url if raw_url.startswith("http") else f"https://{raw_url}"
        self._pending_alias_site_id = base_url.replace("https://", "").replace("http://", "").rstrip("/")

        return f"{_PASSTHROUGH}{response}"

    # ── User provides aliases ─────────────────────────────────────────────

    def handle_add_aliases(self, alias_input: str, run_query: Callable[[str], str]) -> str:
        """Handle alias list reply. Stores aliases and re-runs original query."""
        site_id        = self._pending_alias_site_id
        original_query = self._original_query_after_teach
        self._pending_alias_site_id      = None
        self._original_query_after_teach = None

        clean = alias_input.lower().strip().rstrip(".!?")
        if clean in _NO_ALIAS or len(clean) < 2:
            return run_query(original_query) if original_query else f"{_PASSTHROUGH}Got it, sir."

        parts   = re.split(r"[,\n]+|\band\b", alias_input, flags=re.I)
        aliases = [p.strip().lower() for p in parts if p.strip() and len(p.strip()) > 1]
        for alias in aliases:
            store.add_tag(site_id, alias)
            logger.info("[SiteService] Alias added: {!r} → {}", alias, site_id)

        alias_str = ", ".join(f"'{a}'" for a in aliases)

        if original_query:
            logger.info("[SiteService] Re-running original query after teach: {!r}", original_query)
            return run_query(original_query)

        return f"{_PASSTHROUGH}Got it, sir. I'll also recognise {alias_str} as {site_id}."

    # ── Direct site/alias registration ───────────────────────────────────

    def teach_site(self, url: str, tags: list[str], name: str = "") -> str:
        base_url = url if url.startswith("http") else f"https://{url}"
        site_id  = base_url.replace("https://", "").replace("http://", "").rstrip("/")
        resolver.register_site(site_id, base_url, name, tags)
        logger.info("[SiteService] Site taught: {} | tags: {}", site_id, tags)
        return f"Got it. I'll look for that on {site_id}. Any other names for it?"

    def add_alias(self, site_id: str, alias: str) -> str:
        store.add_tag(site_id, alias.strip().lower())
        return f"Got it — I'll also recognise {alias!r} as {site_id}."
