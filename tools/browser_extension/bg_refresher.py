"""
bg_refresher — per-site portal cache scheduler.

Runs as an asyncio task for the lifetime of JARVIS.  Keeps every registered
portal page fresh in the SQLite cache using the Chrome extension (background
tab extraction) so queries can be answered instantly without Playwright.

Flow:
  1. On boot: load all pages + their refresh intervals from DB.
     If no pages exist (extension never set up) → idle.
  2. Sleep until the earliest overdue page OR _refresh_now event fires.
  3. On wake: for each overdue page, open a background tab via the extension,
     extract all sections, persist to cache.
  4. If a page returns 0 sections → session likely expired:
       - invalidate all cache values for that site
       - update site_health to "expired"
       - enter watch mode: retry every 60s for up to 5 minutes
       - if session comes back → mark "active", narrate "Portal is ready"
  5. Save learned settle_ms on first successful extraction.
  6. Record last_bg_refresh timestamp in site_health.
"""
import asyncio
import threading
from datetime import datetime, timedelta, timezone

from loguru import logger

from tools.web_engine import store
from tools.web_engine.extractor import persist_sections

# Fired by connection_manager when the extension connects — triggers an
# immediate refresh cycle without waiting for the next scheduled time.
_refresh_now: asyncio.Event = asyncio.Event()

# In-memory cache: site_id → session_status ("active"/"expired"/"unknown")
# Accessed from both the asyncio event loop and worker threads; guarded by _status_lock.
_session_status: dict[str, str] = {}
_status_lock = threading.Lock()

# Tracks sites currently in session-watch mode.  Only accessed from the asyncio
# event loop (watch_for_session_recovery is a coroutine), so no extra lock needed.
_watching: set[str] = set()

_WATCH_RETRY_SEC  = 60    # retry interval during session-expired watch mode
_WATCH_MAX_SEC    = 300   # stop watching after 5 minutes


def signal_refresh_now() -> None:
    """Called by connection_manager when the extension connects."""
    _refresh_now.set()


def get_session_status(site_id: str) -> str:
    """Thread-safe reader for in-memory session status."""
    with _status_lock:
        return _session_status.get(site_id, "unknown")


def set_session_status(site_id: str, status: str) -> None:
    """Thread-safe setter — called from both event-loop and worker threads."""
    with _status_lock:
        _session_status[site_id] = status


def is_watching(site_id: str) -> bool:
    """Return True if a watch_for_session_recovery loop is active for site_id.
    Only safe to call from the asyncio event loop thread."""
    return site_id in _watching


async def run(config: dict) -> None:
    """Asyncio task — runs for the lifetime of JARVIS."""
    # Load last-known session states from DB into memory
    health = store.site_health_get_all()
    with _status_lock:
        global _session_status
        _session_status = {sid: h["session_status"] for sid, h in health.items()}
    logger.info("[BG_REFRESH] Loaded session state for {} site(s)", len(_session_status))

    # Boot delay: let WebSocket server start and extension connect
    await asyncio.sleep(30)

    while True:
        pages = store.get_pages_for_refresh()

        if not pages:
            # No pages registered yet — check again after a long sleep
            logger.debug("[BG_REFRESH] No pages in DB — sleeping 10 min")
            await asyncio.sleep(600)
            continue

        # Compute seconds until the next page is due
        now      = datetime.now(timezone.utc)
        next_due = _seconds_until_next(pages, now)

        # H1 FIX: removed asyncio.shield — it leaked an orphaned Task on every
        # normal timeout (one per scheduler cycle). Plain wait_for() cancels cleanly.
        try:
            await asyncio.wait_for(_refresh_now.wait(), timeout=next_due)
            _refresh_now.clear()
            logger.info("[BG_REFRESH] Triggered immediately — extension just connected")
        except asyncio.TimeoutError:
            logger.debug("[BG_REFRESH] Schedule elapsed — starting refresh cycle")

        await _refresh_cycle(pages)


def _seconds_until_next(pages: list[dict], now: datetime) -> float:
    """Return seconds until the earliest overdue page. Minimum 60s."""
    soonest = None
    for p in pages:
        interval_min = p.get("refresh_interval_minutes") or 60
        last_str     = p.get("last_validated_at")
        if not last_str:
            return 60.0  # never validated → overdue immediately
        try:
            last = datetime.fromisoformat(last_str)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            due  = last + timedelta(minutes=interval_min)
            wait = (due - now).total_seconds()
        except Exception:
            return 60.0
        if soonest is None or wait < soonest:
            soonest = wait

    return max(60.0, soonest) if soonest is not None else 60.0


async def _refresh_cycle(pages: list[dict]) -> None:
    """Run one full refresh pass over all overdue pages."""
    from tools.browser_extension import connection_manager

    if not connection_manager.is_connected():
        logger.debug("[BG_REFRESH] Extension not connected — skipping cycle")
        return

    now              = datetime.now(timezone.utc)
    interval_by_page = {p["id"]: (p.get("refresh_interval_minutes") or 60) for p in pages}

    overdue = []
    for p in pages:
        last_str = p.get("last_validated_at")
        if not last_str:
            overdue.append(p)
            continue
        try:
            last = datetime.fromisoformat(last_str)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now >= last + timedelta(minutes=interval_by_page[p["id"]]):
                overdue.append(p)
        except Exception:
            overdue.append(p)

    if not overdue:
        logger.debug("[BG_REFRESH] No pages overdue — cycle done")
        return

    logger.info("[BG_REFRESH] Refreshing {} overdue page(s)", len(overdue))

    for page_row in overdue:
        if not connection_manager.is_connected():
            logger.info("[BG_REFRESH] Extension disconnected mid-cycle — stopping")
            break
        await _refresh_page(page_row)


async def _refresh_page(page_row: dict) -> None:
    """Extract one page via the extension and update cache + site_health."""
    from tools.browser_extension.commands import extract_page as ep_cmd

    site_id   = page_row["site_id"]
    page_id   = page_row["id"]
    url       = page_row["url"]
    settle_ms = page_row.get("settle_ms")

    logger.info("[BG_REFRESH] Extracting {!r} (settle_ms={})", url, settle_ms)

    try:
        result = await ep_cmd.run(url, settle_ms=settle_ms)
    except Exception as exc:
        logger.warning("[BG_REFRESH] extract_page failed for {!r}: {}", url, exc)
        return

    sections   = result.get("sections", [])
    learned_ms = result.get("learned_ms")

    if not sections:
        await _handle_empty(site_id, page_id, url)
        return

    # H2 FIX: persist_sections() is sync (SQLite writes + OpenAI HTTP).  Run it
    # off the event loop so WebSocket heartbeats and command dispatch don't stall.
    loop = asyncio.get_running_loop()
    meaningful = await loop.run_in_executor(None, persist_sections, sections, site_id, page_id, url)
    await loop.run_in_executor(None, store.mark_page_validated, page_id)

    if learned_ms:
        await loop.run_in_executor(None, store.save_page_settle_ms, page_id, learned_ms)
        logger.info("[BG_REFRESH] Learned settle_ms={} for {!r}", learned_ms, url)

    count = len(meaningful)
    await loop.run_in_executor(
        None, lambda: store.site_health_upsert(site_id, "active", last_section_count=count)
    )
    set_session_status(site_id, "active")

    logger.info("[BG_REFRESH] {!r} — {} section(s) cached", url, count)


async def _handle_empty(site_id: str, page_id: str, url: str) -> None:
    """0 sections returned — treat as expired only if page was previously known to have sections."""
    # H5 FIX: don't mark a brand-new or legitimately empty page as session-expired.
    if not store.site_has_sections(site_id):
        logger.info(
            "[BG_REFRESH] 0 sections for {!r} but site has no prior history — skipping expiry", url
        )
        return

    prev = get_session_status(site_id)
    logger.warning(
        "[BG_REFRESH] 0 sections for {!r} (prev status={!r}) — invalidating cache",
        url, prev,
    )
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, store.invalidate_site_cache, site_id)
    await loop.run_in_executor(
        None, lambda: store.site_health_upsert(site_id, "expired", last_section_count=0)
    )
    set_session_status(site_id, "expired")


async def watch_for_session_recovery(site_id: str, url: str, narration=None) -> None:
    """
    Called after JARVIS tells the user to log back in.
    Retries the page every 60s for up to 5 minutes.
    Narrates "Portal is ready" when session comes back.
    """
    from tools.browser_extension import connection_manager
    from tools.browser_extension.commands import extract_page as ep_cmd

    if site_id in _watching:
        logger.debug("[BG_REFRESH] Watch: already watching {!r} — ignoring duplicate", site_id)
        return

    _watching.add(site_id)
    logger.info("[BG_REFRESH] Watch mode: checking {!r} for session recovery", url)

    deadline = datetime.now(timezone.utc) + timedelta(seconds=_WATCH_MAX_SEC)

    try:
        while datetime.now(timezone.utc) < deadline:
            if not connection_manager.is_connected():
                logger.debug("[BG_REFRESH] Watch: extension disconnected — stopping watch")
                return

            try:
                result = await ep_cmd.run(url, settle_ms=None)
            except Exception as exc:
                logger.debug("[BG_REFRESH] Watch: extraction error {}", exc)
                await asyncio.sleep(_WATCH_RETRY_SEC)
                continue

            sections = result.get("sections", [])
            if sections:
                logger.info("[BG_REFRESH] Watch: session recovered for {!r} — {} section(s)", url, len(sections))

                # H6 FIX: persist recovered sections so the next user query is answered
                # from cache instead of triggering another extraction cycle.
                loop = asyncio.get_running_loop()
                page_row = await loop.run_in_executor(None, store.get_page_by_url, site_id, url)
                if page_row:
                    await loop.run_in_executor(
                        None, persist_sections, sections, site_id, page_row["id"], url
                    )
                    await loop.run_in_executor(None, store.mark_page_validated, page_row["id"])

                set_session_status(site_id, "active")
                count = len(sections)
                await loop.run_in_executor(
                    None, lambda: store.site_health_upsert(site_id, "active", last_section_count=count)
                )
                if narration:
                    narration.say("Portal is ready, sir.")
                return

            logger.debug("[BG_REFRESH] Watch: still no sections — retrying in {}s", _WATCH_RETRY_SEC)
            await asyncio.sleep(_WATCH_RETRY_SEC)

        logger.warning("[BG_REFRESH] Watch: 5-minute window elapsed for {!r} — stopping", url)
    finally:
        _watching.discard(site_id)
