"""
WebEngine: main entry point. Called by tools/registry.py.

Handles the full lifecycle:
  resolve site → read/write intent → retrieve (cached) → discover (new) → write ops
"""
import re
import threading

from loguru import logger

from tools.web_engine import store, resolver, retriever, discoverer, validator
from tools.web_engine.actions import form_submit


_PASSTHROUGH     = "__PASSTHROUGH__:"
_PORTAL_TIMEOUT  = "__PORTAL_TIMEOUT__"


class WebEngine:
    def __init__(self, narration, config: dict):
        self.narration = narration
        self._pending_query: tuple[str, str] | None = None  # (query, site_id) waiting for user confirmation
        store.init_db()
        # Validator uses sync Playwright — must run in a thread, not in the asyncio loop
        threading.Thread(target=validator.run_if_due, daemon=True).start()
        _seed_portal(config)

    def run(self, params: dict) -> str:
        # ── Pending confirmation ─────────────────────────────────────────
        if params.get("action") == "confirm_pending":
            if self._pending_query:
                q, s = self._pending_query
                store.add_pending_query(q, s)
                self._pending_query = None
                logger.info("[ENGINE] Pending query saved: {!r}", q)
                return f"{_PASSTHROUGH}I've made a note of it. I'll keep an eye on the portal and let you know as soon as I find it, sir."
            return f"{_PASSTHROUGH}There's no pending query saved, sir."

        query = params.get("text", "").strip()
        if not query:
            return "I didn't catch what you wanted to find. Could you say that again?"

        logger.info("[ENGINE] Query: {!r}", query)

        # ── Resolve site ────────────────────────────────────────────────────
        # Accept a caller-provided site_id for temporal follow-ups whose text
        # ("previous month?") carries no resolvable site signal.
        site_id_hint = params.get("site_id")
        site_id, intent = resolver.resolve(query)
        if site_id is None and site_id_hint:
            site_id = site_id_hint
            intent  = "read"
            logger.info("[ENGINE] Using caller-provided site_id hint: {}", site_id)

        if site_id is None:
            return self._ask_for_site(query)

        # ── Write intent ────────────────────────────────────────────────────
        if intent == "write":
            return self._handle_write(site_id, query)

        # ── Read intent ─────────────────────────────────────────────────────
        self.narration.step("Let me look that up...")

        # Skip cache for time-specific queries (month navigation needed)
        from tools.web_engine.discoverer import _extract_month_target
        needs_fresh = _extract_month_target(query) is not None

        if not needs_fresh:
            answer = retriever.retrieve(query, site_id)
            if answer:
                return answer

        # Unknown or time-specific — discover
        logger.info("[ENGINE] Discovering for query: {!r}", query)
        self.narration.step("I haven't seen this before — let me find it.")
        answer = discoverer.discover(query, site_id, self.narration)
        if answer == _PORTAL_TIMEOUT:
            self._pending_query = (query, site_id)
            logger.info("[ENGINE] Portal timeout — asking user if they want background check")
            return (
                f"{_PASSTHROUGH}The portal seems slow right now. "
                "Want me to keep checking and let you know when I find it, sir?"
            )
        if answer:
            return answer

        return (
            "I couldn't find that data on the portal. "
            "If you tell me which page it's on, I can look there directly."
        )

    # ──────────────────────────────────────────────
    # Site teaching
    # ──────────────────────────────────────────────

    def _ask_for_site(self, query: str) -> str:
        """
        JARVIS doesn't know which site to use.
        Returns a question for the user; the response is handled by the next
        command cycle (the task_router sees it as a follow-up).
        For now, return a prompt and store the pending context.
        """
        auto_tags = resolver.extract_tags_from_query(query)
        logger.info("[ENGINE] Unknown site for query {!r} — asking user", query)
        tag_hint = f" ({', '.join(auto_tags)})" if auto_tags else ""
        return (
            f"I don't know which website{tag_hint} has that information. "
            f"Could you give me the URL?"
        )

    def teach_site(self, url: str, tags: list[str], name: str = "") -> str:
        """
        Register a new site (called when the user provides a URL in follow-up).
        """
        base_url = url if url.startswith("http") else f"https://{url}"
        site_id  = base_url.replace("https://", "").replace("http://", "").rstrip("/")
        resolver.register_site(site_id, base_url, name, tags)
        logger.info("[ENGINE] Site taught: {} | tags: {}", site_id, tags)
        return f"Got it. I'll look for that on {site_id}. Any other names for it?"

    def add_alias(self, site_id: str, alias: str) -> str:
        store.add_tag(site_id, alias.strip().lower())
        return f"Got it — I'll also recognise {alias!r} as {site_id}."

    # ──────────────────────────────────────────────
    # Write operations
    # ──────────────────────────────────────────────

    def _handle_write(self, site_id: str, query: str) -> str:
        # Detect EOD submission
        eod_match = re.search(
            r"(?:submit|send|log|write|record)\s+(?:my\s+)?(?:eod|end[- ]of[- ]day|work journal)[:\s]+(.+)",
            query, re.I
        )
        if eod_match:
            text = eod_match.group(1).strip()
            self.narration.step("Opening work journal...")
            return form_submit.run(site_id, "form_submit_eod", {"text": text})

        # Generic: open visible browser for user to interact
        site = store.get_site(site_id)
        if site:
            self.narration.step(f"Opening {site['name'] or site_id} for you...")
        return f"Please complete this action in the browser that's opening now."


# ──────────────────────────────────────────────────────────────────────────────
# Bootstrap: seed the portal site + EOD action so it works on day one
# without requiring explicit "teach me the portal" flow.
# ──────────────────────────────────────────────────────────────────────────────

_PORTAL_SITE_ID = "people.codeclouds.com"
_PORTAL_BASE    = "https://people.codeclouds.com"
_PORTAL_TAGS    = [
    # Site identity
    "office portal", "hr portal", "portal", "simplified hr",
    "paipa", "codeclouds", "people", "work portal",
    # HR data keywords — so queries like "my attendance" pre-route here without LLM
    "attendance", "punctuality", "leave", "leaves", "casual leave", "sick leave",
    "activity", "activity rate", "activity percentage", "eod", "end of day",
    "work journal", "seat booking", "seat", "support ticket", "request",
    "salary", "payslip", "holiday", "timesheet", "check in", "check out",
]

def _seed_portal(config: dict) -> None:
    """Register the office portal and ensure all tags are present."""
    existing = store.get_site(_PORTAL_SITE_ID)
    if not existing:
        store.upsert_site(_PORTAL_SITE_ID, _PORTAL_BASE, "Simplified HR Portal")

    # Always sync tags — idempotent (INSERT OR IGNORE)
    for tag in _PORTAL_TAGS:
        store.add_tag(_PORTAL_SITE_ID, tag)

    # Seed EOD form action
    store.upsert_action(_PORTAL_SITE_ID, "form_submit_eod", {
        "url":    "/my-apps/work-journal",
        "fields": [{"selector": "textarea", "value": "{text}"}],
        "submit": "button[type='submit']",
        "wait_ms": 3000,
    })

    # Seed read page hints — used by discoverer to navigate directly to the right page
    store.upsert_action(_PORTAL_SITE_ID, "page_activity", {
        "url":      "/my-activity",
        "keywords": ["attendance", "activity", "punctuality", "check in", "check out",
                     "active hours", "activity rate", "activity percentage"],
    })
    store.upsert_action(_PORTAL_SITE_ID, "page_leave", {
        "url":      "/leave",
        "keywords": ["leave", "leaves", "casual leave", "sick leave", "leave balance",
                     "leave remaining", "leave count"],
    })
    store.upsert_action(_PORTAL_SITE_ID, "page_requests", {
        "url":      "/requests",
        "keywords": ["request", "ticket", "support ticket", "support request"],
    })
    store.upsert_action(_PORTAL_SITE_ID, "page_work_journal", {
        "url":      "/my-apps/work-journal",
        "keywords": ["work journal", "journal", "today task", "today work"],
    })

    # Migrate session from legacy portal_session.json if it exists
    import json
    from pathlib import Path
    legacy = Path("data/portal_session.json")
    if legacy.exists():
        try:
            session_data = json.loads(legacy.read_text(encoding="utf-8"))
            store.save_session(_PORTAL_SITE_ID, session_data)
            logger.info("[ENGINE] Migrated legacy portal session to SQLite")
        except Exception as e:
            logger.warning("[ENGINE] Could not migrate legacy session: {}", e)

    logger.info("[ENGINE] Portal seeded with {} tags", len(_PORTAL_TAGS))
