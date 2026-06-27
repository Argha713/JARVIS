"""
WebEngine: thin orchestrator. Called by tools/registry.py.

Delegates to:
  services/query_router.py   — read-path decision chain
  services/site_service.py   — site teaching / alias state machine
  services/intent_service.py — EOD / write-sub-intent detection
  discovery_engine.py        — user-triggered page discovery ("watch <URL>")
"""
import threading

from loguru import logger

from tools.web_engine import store, resolver, validator
from tools.web_engine.actions import form_submit
from tools.web_engine.services import (
    intent_service,
    query_router,
)
from tools.web_engine.services.site_service import SiteService

_PASSTHROUGH = "__PASSTHROUGH__:"


class WebEngine:
    def __init__(self, narration, config: dict):
        self.narration   = narration
        self._config     = config
        self._site_svc   = SiteService()
        self._pending_query: tuple[str, str] | None = None
        self._last_page_url: dict[str, str] = {}

        store.init_db()

        # Skip Playwright validator if the extension was ever installed — bg_refresher
        # takes over cache freshness when Chrome opens.  New users (no extension set up)
        # still get the Playwright validator as before.
        #
        # Boot-ordering note: extension_installed_any() reads the DB, not the live
        # WebSocket state.  The WS handshake hasn't happened yet at this point, so
        # we can't rely on connection_manager.is_connected().  A returning user who
        # has the extension installed but hasn't opened Chrome yet will still skip
        # the validator — bg_refresher will refresh once the extension connects.
        if store.extension_installed_any():
            logger.info("[ENGINE] Extension previously installed — skipping Playwright validator")
        else:
            threading.Thread(target=validator.run_if_due, daemon=True).start()

    # ── Main entry (called by registry) ──────────────────────────────────

    def run(self, params: dict) -> str:
        recorder = params.get("_recorder")
        action   = params.get("action")

        logger.info("[ENGINE] ══════════════════════════════════════")
        logger.info("[ENGINE] run() called: action={!r} recorder={}", action, recorder is not None)

        # ── Confirm pending ───────────────────────────────────────────────
        if action == "confirm_pending":
            logger.info("[ENGINE] Branch: confirm_pending")
            return self._handle_confirm_pending()

        query = params.get("text", "").strip()
        logger.info("[ENGINE] query={!r}", query)

        if not query:
            logger.warning("[ENGINE] Empty query — returning early")
            return "I didn't catch what you wanted to find. Could you say that again?"

        # ── Special action dispatch ───────────────────────────────────────
        logger.debug("[ENGINE] Checking special actions: action={!r}", action)
        if action == "discover_site":
            logger.info("[ENGINE] Branch: discover_site url={!r}", query)
            return self._handle_discover_site(query)

        if action == "teach_tag":
            logger.info("[ENGINE] Branch: teach_tag")
            return self._site_svc.handle_teach_tag(query)
        if action == "refresh_knowledge":
            logger.info("[ENGINE] Branch: refresh_knowledge")
            return self._handle_refresh_knowledge()
        if action == "teach_site":
            logger.info("[ENGINE] Branch: teach_site")
            return self._site_svc.handle_teach_site(query)
        if action == "add_aliases":
            logger.info("[ENGINE] Branch: add_aliases")
            return self._site_svc.handle_add_aliases(query, lambda q: self.run({"text": q}))

        # ── Resolve site ──────────────────────────────────────────────────
        logger.debug("[ENGINE] Resolving site from query …")
        site_id, intent = resolver.resolve(query)
        site_id_hint    = params.get("site_id")

        logger.info("[ENGINE] resolver.resolve → site_id={!r} intent={!r}", site_id, intent)

        if site_id is None and site_id_hint:
            site_id, intent = site_id_hint, "read"
            logger.info("[ENGINE] site_id was None — using caller hint: site_id={!r}", site_id)

        if site_id is None:
            logger.info("[ENGINE] site_id still None — asking user for URL (site_svc.ask_for_site)")
            logger.info("[ENGINE] SiteService state: awaiting_site_url={} awaiting_aliases={}",
                        self._site_svc.awaiting_site_url, self._site_svc.awaiting_aliases)
            return self._site_svc.ask_for_site(query)

        logger.info("[ENGINE] site_id resolved: {!r}", site_id)

        # ── Write intent ──────────────────────────────────────────────────
        if intent == "write":
            logger.info("[ENGINE] Branch: WRITE intent → _handle_write")
            return self._handle_write(site_id, query, recorder)

        # ── Read intent ───────────────────────────────────────────────────
        logger.info("[ENGINE] Branch: READ intent → query_router.route")
        logger.debug("[ENGINE] last_page_url cache: {}", self._last_page_url)
        self.narration.step("Let me look that up...")

        answer, page_url = query_router.route(
            query, site_id, self.narration, self._last_page_url, recorder
        )
        logger.info("[ENGINE] query_router returned: answer={} page_url={!r}",
                    f"len={len(answer)}" if answer and answer != query_router.PORTAL_TIMEOUT else repr(answer),
                    page_url)

        if page_url:
            logger.debug("[ENGINE] Caching page_url for site {!r}: {!r}", site_id, page_url)
            self._last_page_url[site_id] = page_url

        if answer == query_router.PORTAL_TIMEOUT:
            self._pending_query = (query, site_id)
            logger.info("[ENGINE] Portal timeout — storing pending query and asking user")
            return (
                f"{_PASSTHROUGH}The portal seems slow right now. "
                "Want me to keep checking and let you know when I find it, sir?"
            )

        if answer:
            logger.info("[ENGINE] Returning answer (len={}): {!r}", len(answer), answer[:80])
        else:
            logger.warning("[ENGINE] No answer from any path — returning fallback message")

        logger.info("[ENGINE] ══════════════════════════════════════")
        return answer or (
            "I couldn't find that data on the portal. "
            "If you tell me which page it's on, I can look there directly."
        )

    # ── Write handler ─────────────────────────────────────────────────────

    def _handle_write(self, site_id: str, query: str, recorder=None) -> str:
        logger.debug("[ENGINE] _handle_write: site={!r} query={!r}", site_id, query[:60])
        eod_text = intent_service.extract_eod_text(query)

        if eod_text:
            logger.info("[ENGINE] Write sub-intent: EOD — eod_text={!r}", eod_text[:60])
            self.narration.step("Opening work journal...")
            result = form_submit.run(site_id, "form_submit_eod", {"text": eod_text}, recorder=recorder)
            logger.info("[ENGINE] form_submit.run returned: {!r}", result[:80])
            logger.info("[ENGINE] ══════════════════════════════════════")
            return result

        logger.info("[ENGINE] Write sub-intent: GENERIC — opening browser for user interaction")
        site = store.get_site(site_id)
        logger.debug("[ENGINE] site record: {}", site)
        if site:
            self.narration.step(f"Opening {site['name'] or site_id} for you...")
        logger.info("[ENGINE] ══════════════════════════════════════")
        return "Please complete this action in the browser that's opening now."

    # ── Misc handlers ─────────────────────────────────────────────────────

    def _handle_confirm_pending(self) -> str:
        logger.debug("[ENGINE] _handle_confirm_pending: pending_query={}", self._pending_query)
        if self._pending_query:
            q, s = self._pending_query
            store.add_pending_query(q, s)
            self._pending_query = None
            logger.info("[ENGINE] Pending query saved: site={!r} query={!r}", s, q)
            logger.info("[ENGINE] ══════════════════════════════════════")
            return (
                f"{_PASSTHROUGH}I've made a note of it. I'll keep an eye on the portal "
                "and let you know as soon as I find it, sir."
            )
        logger.info("[ENGINE] No pending query to confirm")
        logger.info("[ENGINE] ══════════════════════════════════════")
        return f"{_PASSTHROUGH}There's no pending query saved, sir."

    def _handle_discover_site(self, url: str) -> str:
        """Fire-and-forget: schedule discovery_engine.discover() on the main loop."""
        import asyncio
        from tools.browser_extension import _main_loop
        from tools.web_engine.discovery_engine import discover

        if not _main_loop or not _main_loop.is_running():
            return "I can't start discovery right now — the event loop isn't ready, sir."

        asyncio.run_coroutine_threadsafe(
            discover(url, self.narration, self._config),
            _main_loop,
        )
        logger.info("[ENGINE] Discovery scheduled for {!r}", url)
        return f"__PASSTHROUGH__Starting discovery for {url}, sir. I'll explore the page and let you know when I'm done."

    def _handle_refresh_knowledge(self) -> str:
        logger.info("[ENGINE] Manual knowledge refresh triggered — starting validator thread")
        threading.Thread(target=validator.run_full, args=(self.narration,), daemon=True).start()
        logger.info("[ENGINE] ══════════════════════════════════════")
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
