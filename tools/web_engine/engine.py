"""
WebEngine: thin orchestrator. Called by tools/registry.py.

Delegates to:
  services/query_router.py  — read-path decision chain
  services/site_service.py  — site teaching / alias state machine
  services/intent_service.py — EOD / write-sub-intent detection
  services/portal_seeder.py  — site seeding on startup
"""
import threading

from loguru import logger

from tools.web_engine import store, resolver, validator
from tools.web_engine.actions import form_submit
from tools.web_engine.services import (
    intent_service,
    portal_seeder,
    query_router,
)
from tools.web_engine.services.site_service import SiteService

_PASSTHROUGH = "__PASSTHROUGH__:"


class WebEngine:
    def __init__(self, narration, config: dict):
        self.narration   = narration
        self._site_svc   = SiteService()
        self._pending_query: tuple[str, str] | None = None   # (query, site_id) for background check
        self._last_page_url: dict[str, str] = {}             # site_id → last discovered page URL

        store.init_db()
        threading.Thread(target=validator.run_if_due, daemon=True).start()
        portal_seeder.seed(config)

    # ── Main entry (called by registry) ──────────────────────────────────

    def run(self, params: dict) -> str:
        recorder = params.get("_recorder")

        if params.get("action") == "confirm_pending":
            return self._handle_confirm_pending()

        query = params.get("text", "").strip()
        if not query:
            return "I didn't catch what you wanted to find. Could you say that again?"

        logger.info("[ENGINE] Query: {!r}", query)

        # ── Special action dispatch ────────────────────────────────────────
        action = params.get("action")
        if action == "teach_tag":
            return self._site_svc.handle_teach_tag(query)
        if action == "refresh_knowledge":
            return self._handle_refresh_knowledge()
        if action == "teach_site":
            return self._site_svc.handle_teach_site(query)
        if action == "add_aliases":
            return self._site_svc.handle_add_aliases(query, lambda q: self.run({"text": q}))

        # ── Resolve site ─────────────────────────────────────────────────
        site_id, intent = resolver.resolve(query)
        site_id_hint    = params.get("site_id")
        if site_id is None and site_id_hint:
            site_id, intent = site_id_hint, "read"
            logger.info("[ENGINE] Using caller-provided site_id hint: {}", site_id)

        if site_id is None:
            return self._site_svc.ask_for_site(query)

        # ── Write intent ─────────────────────────────────────────────────
        if intent == "write":
            return self._handle_write(site_id, query, recorder)

        # ── Read intent → delegate to query_router ────────────────────────
        self.narration.step("Let me look that up...")
        answer, page_url = query_router.route(
            query, site_id, self.narration, self._last_page_url, recorder
        )

        if page_url:
            self._last_page_url[site_id] = page_url

        if answer == query_router.PORTAL_TIMEOUT:
            self._pending_query = (query, site_id)
            logger.info("[ENGINE] Portal timeout — asking user for background check")
            return (
                f"{_PASSTHROUGH}The portal seems slow right now. "
                "Want me to keep checking and let you know when I find it, sir?"
            )

        return answer or (
            "I couldn't find that data on the portal. "
            "If you tell me which page it's on, I can look there directly."
        )

    # ── Write handler ─────────────────────────────────────────────────────

    def _handle_write(self, site_id: str, query: str, recorder=None) -> str:
        eod_text = intent_service.extract_eod_text(query)
        if eod_text:
            self.narration.step("Opening work journal...")
            return form_submit.run(site_id, "form_submit_eod", {"text": eod_text}, recorder=recorder)
        site = store.get_site(site_id)
        if site:
            self.narration.step(f"Opening {site['name'] or site_id} for you...")
        return "Please complete this action in the browser that's opening now."

    # ── Misc handlers ─────────────────────────────────────────────────────

    def _handle_confirm_pending(self) -> str:
        if self._pending_query:
            q, s = self._pending_query
            store.add_pending_query(q, s)
            self._pending_query = None
            logger.info("[ENGINE] Pending query saved: {!r}", q)
            return (
                f"{_PASSTHROUGH}I've made a note of it. I'll keep an eye on the portal "
                "and let you know as soon as I find it, sir."
            )
        return f"{_PASSTHROUGH}There's no pending query saved, sir."

    def _handle_refresh_knowledge(self) -> str:
        threading.Thread(target=validator.run_full, args=(self.narration,), daemon=True).start()
        logger.info("[ENGINE] Manual knowledge refresh triggered")
        return f"{_PASSTHROUGH}I'll update everything in the background, sir."

    # ── Public helpers (backward-compatible with task_router) ─────────────

    def teach_site(self, url: str, tags: list[str], name: str = "") -> str:
        return self._site_svc.teach_site(url, tags, name)

    def add_alias(self, site_id: str, alias: str) -> str:
        return self._site_svc.add_alias(site_id, alias)

    # ── Properties exposed so task_router can check pending state ─────────

    @property
    def _pending_site_query(self) -> str | None:
        return self._site_svc._pending_site_query

    @property
    def _pending_alias_site_id(self) -> str | None:
        return self._site_svc._pending_alias_site_id
