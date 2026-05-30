"""
Discoverer: handles first-run discovery of unknown data.

Foreground (guided):
  - Opens visible browser, logs in if needed
  - Tries to navigate autonomously using semantic nav-link scoring
  - Asks narration/user if stuck
  - Extracts full page, returns answer

Background enrichment (after answer is spoken):
  - Clicks each tab on the current page
  - Extracts sections from each tab
  - No user interaction
"""
import json as _json
import re
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlunparse

from loguru import logger
from playwright.sync_api import Page, sync_playwright

# URLs currently being enriched in the background — prevents duplicate threads
_enriching: set[str] = set()

from tools.web_engine import store
from tools.web_engine.actions import login as login_action
from tools.web_engine.actions import navigate
from tools.web_engine.extractor import extract_page

# Minimum semantic score to auto-navigate (without asking).
# MiniLM cosine similarity for semantically related but not identical phrases
# (e.g. "My Activity" vs "punctuality rate") is typically 0.35–0.50.
_AUTO_NAV_THRESHOLD = 0.35


def discover(query: str, site_id: str, narration,
             start_url: str | None = None) -> tuple[str | None, str | None]:
    """
    Foreground guided discovery.
    Opens visible browser, finds data, returns (answer, page_url) or (None, None).
    start_url: if provided, go there directly — skips home-page navigation logic.
               Used by temporal follow-ups so they land on /my-activity (not home).
    """
    site = store.get_site(site_id)
    if not site:
        logger.error("[DISCOVERER] Unknown site: {}", site_id)
        return None, None

    base_url = site["base_url"]

    # Ensure login
    ok = login_action.ensure_session(site_id, narration)
    if not ok:
        return "Login failed or timed out.", None

    narration.say("Let me find that for you, sir.")

    session = store.load_session(site_id)
    ctx_kwargs = {"storage_state": session} if session else {}

    # Caller may provide a start_url (temporal follow-ups reuse the last known page).
    # Otherwise fall back to the normal hint chain.
    if start_url is None:
        start_url = (
            _find_known_page_url(query, site_id)
            or _get_direct_url_hint(query, site_id, base_url)
            or base_url
        )

    current_url = None
    answer = None
    _first_visit = not store.site_has_sections(site_id)
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=not _first_visit, slow_mo=80 if _first_visit else 0)
        context = browser.new_context(**ctx_kwargs)
        page    = context.new_page()

        # Intercept all JSON responses — captures URL, HTTP method, POST body, and response data
        _captured_api: dict = {}
        def _on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct and response.status == 200:
                    method = response.request.method
                    body = None
                    if method == "POST":
                        try:
                            body = response.request.post_data  # raw POST body string
                        except Exception:
                            pass
                    _captured_api[response.url] = {
                        "data":   response.json(),
                        "method": method,
                        "body":   body,
                    }
            except Exception:
                pass
        page.on("response", _on_response)

        try:
            page.goto(start_url, timeout=35_000, wait_until="domcontentloaded")
        except Exception:
            logger.warning("[DISCOVERER] goto timed out — retrying with 60s")
            try:
                page.goto(start_url, timeout=60_000, wait_until="commit")
            except Exception as retry_err:
                logger.error("[DISCOVERER] Portal unreachable after retry: {}", retry_err)
                browser.close()
                return "__PORTAL_TIMEOUT__", None
        _wait_for_content(page)
        logger.info("[DISCOVERER] Starting discovery from: {}", page.url)

        # Try to navigate further if still not on the right page
        if start_url == base_url:
            page = _navigate_to_data(page, query, site_id, narration)

        # Navigate to a historical month if the query asks for one
        target_month = _extract_month_target(query)
        if target_month:
            logger.info("[DISCOVERER] Query requests month: {}", target_month.strftime("%B %Y"))
            if _navigate_to_target_month(page, target_month):
                _wait_for_content(page)
                logger.info("[DISCOVERER] Arrived at {}", target_month.strftime("%B %Y"))
            else:
                logger.warning("[DISCOVERER] Could not navigate to {} — extracting current view",
                               target_month.strftime("%B %Y"))

        # Extract full page
        current_url = page.url
        page_id = store.upsert_page(site_id, current_url)
        _save_captured_endpoints(site_id, current_url, _captured_api)
        sections = extract_page(page, site_id, page_id, current_url)

        if sections:
            if _first_visit:
                narration.say("I'm learning this page in the background.")
            # Cache all extracted values
            for sec in sections:
                from tools.web_engine import store as s
                sect_id = s.upsert_section(page_id, sec["label"], sec.get("selector", ""))
                s.set_cache(sect_id, sec["value"])

            # Find best matching section
            hits = store.semantic_search(query, site_id=site_id, n=3)
            if hits:
                logger.info("[DISCOVERER] Post-extraction search hits (threshold={}):",
                            _AUTO_NAV_THRESHOLD)
                for i, h in enumerate(hits):
                    above = "✓" if h["score"] >= _AUTO_NAV_THRESHOLD else "✗"
                    logger.info("[DISCOVERER]   #{} [{}] score={:.3f} label={!r} sid={}",
                                i + 1, above, h["score"], h["label"][:50], h["section_id"][:8])
                best = hits[0]
                if best["score"] < _AUTO_NAV_THRESHOLD:
                    logger.warning(
                        "[DISCOVERER] Best score {:.3f} is BELOW threshold {:.2f} — "
                        "label={!r} — returning None to avoid wrong answer",
                        best["score"], _AUTO_NAV_THRESHOLD, best["label"][:50]
                    )
                    # answer stays None — engine will say "I couldn't find that"
                else:
                    raw = store.get_cache(best["section_id"])
                    logger.info("[DISCOVERER] Selected {} label={!r} score={:.3f} cache={}",
                                best["section_id"][:8], best["label"], best["score"],
                                repr(raw[:40]) if raw else "MISS")
                    if raw:
                        answer = store.format_with_history(best["section_id"], raw)
                    else:
                        answer = best["document"]
                    logger.info("[DISCOVERER] Answer → {!r}", answer[:120])
        else:
            logger.warning("[DISCOVERER] No sections extracted from {!r}", current_url)

        # Background tab enrichment
        _start_background_enrichment(site_id, page_id, current_url, session)

        browser.close()
    except Exception as e:
        logger.exception("[DISCOVERER] Error: {}", e)
    finally:
        try:
            pw.stop()
        except Exception:
            pass

    return answer, current_url


def _navigate_to_data(page: Page, query: str, site_id: str, narration) -> Page:
    """
    Tries to navigate autonomously using nav-link semantic scoring.
    Asks narration for help if no confident match found.
    Returns the page (may be the same page if navigation not needed or failed).
    """
    for _attempt in range(3):   # max 3 navigation hops
        nav_links = _extract_nav_links(page)
        if not nav_links:
            break

        # Score each link against the query
        from tools.web_engine.store import semantic_search, _chroma_collection
        # Use ChromaDB to embed query and score link texts
        scored = _score_links(query, nav_links)
        logger.info("[DISCOVERER] Nav scoring top-5: {}",
                    [(t, f"{s:.2f}") for t, _, s in scored[:5]])

        if scored and scored[0][2] >= _AUTO_NAV_THRESHOLD:
            best_text, best_url, best_score = scored[0]
            logger.info("[DISCOVERER] Auto-navigating to {!r} (score={:.2f})",
                        best_text, best_score)
            try:
                page.goto(best_url, timeout=25_000, wait_until="domcontentloaded")
                page.wait_for_timeout(1_500)
                store.upsert_page(site_id, page.url, best_text)
            except Exception as e:
                logger.warning("[DISCOVERER] Navigation to {!r} failed: {}", best_url, e)
            break
        else:
            # Not confident — ask narration
            link_names = [t for t, _, _ in scored[:5]]
            options_str = ", ".join(link_names) if link_names else "nothing useful"
            narration.say(
                f"I can see these sections: {options_str}. "
                f"Which one has the information you're looking for?"
            )
            # Wait for user guidance — in practice the user's next command
            # will come through the normal wake-word flow. For now, stop here.
            logger.info("[DISCOVERER] Waiting for user guidance. Options: {}", link_names)
            break

    return page


def _extract_nav_links(page: Page) -> list[tuple[str, str]]:
    """
    Returns [(link_text, absolute_url), ...] for all navigation-like links on the page.

    Strategy 1: anchors inside known nav/menu containers.
    Strategy 2 (fallback): all same-origin <a> tags with short text.
    This avoids the common mistake of selecting container elements (li, div) that
    lack an href attribute.
    """
    try:
        links = page.evaluate("""() => {
            const results = [];
            const seen = new Set();
            const origin = window.location.origin;
            const currentPath = window.location.pathname;

            // Resolve a raw href/data-href value to an absolute same-origin URL.
            // Returns '' if it doesn't resolve to the same origin.
            function resolveHref(raw) {
                if (!raw) return '';
                try {
                    const url = new URL(raw, origin);
                    return url.origin === origin ? url.href : '';
                } catch { return ''; }
            }

            function addLink(el) {
                const text = (el.innerText || el.textContent || '')
                    .replace(/\\s+/g, ' ').trim();
                if (!text || text.length < 2 || text.length > 80) return;

                // Resolve href from: href attribute, data-href, data-url, data-path
                const raw =
                    el.getAttribute('href') ||
                    el.getAttribute('data-href') ||
                    el.getAttribute('data-url') ||
                    el.getAttribute('data-path') || '';
                const href = resolveHref(raw);
                if (!href) return;

                try {
                    const url = new URL(href);
                    if (url.pathname === currentPath) return;
                } catch { return; }

                if (seen.has(text)) return;
                seen.add(text);
                results.push({ text, href });
            }

            // Strategy 1: anchors and role="link" elements inside nav/menu containers
            const navContainers = [
                'nav', 'aside', '[class*="sidebar"]', '[class*="sider"]',
                '[class*="nav"]', '[class*="menu"]',
                '.ant-menu', '.ant-layout-sider',
            ];
            const linkSelectors = 'a, [role="link"], [data-href], [data-url], [data-path]';
            navContainers.forEach(sel => {
                document.querySelectorAll(sel + ' ' + linkSelectors).forEach(addLink);
            });

            // Strategy 2: all same-origin navigable elements (catches SPAs where
            // nav isn't in a semantic container)
            if (results.length === 0) {
                document.querySelectorAll(linkSelectors).forEach(addLink);
            }

            return results.slice(0, 50);
        }""")
        result_list = [(item["text"], item["href"]) for item in (links or [])]
        logger.info("[DISCOVERER] Nav links found on {!r}: {}", page.url, len(result_list))
        return result_list
    except Exception as e:
        logger.debug("[DISCOVERER] Nav link extraction failed: {}", e)
        return []


def _score_links(query: str, links: list[tuple[str, str]]) -> list[tuple[str, str, float]]:
    """
    Score nav links against the query using ChromaDB embeddings.
    Returns [(text, url, score), ...] sorted by score desc.
    """
    if not links:
        return []
    try:
        import chromadb
        client = chromadb.PersistentClient(path="data/chromadb")
        tmp_col = client.get_or_create_collection("nav_scoring_tmp")

        # Index link texts temporarily
        ids   = [f"nav{i}" for i in range(len(links))]
        texts = [t for t, _ in links]
        tmp_col.upsert(ids=ids, documents=texts)

        results = tmp_col.query(query_texts=[query], n_results=min(len(links), 10))
        ids_out  = results.get("ids",       [[]])[0]
        dists    = results.get("distances", [[]])[0]

        scored = []
        for i, nav_id in enumerate(ids_out):
            idx = int(nav_id.replace("nav", ""))
            text, url = links[idx]
            score = 1 - dists[i]
            scored.append((text, url, score))

        # Clean up temp collection
        client.delete_collection("nav_scoring_tmp")
        return sorted(scored, key=lambda x: x[2], reverse=True)
    except Exception as e:
        logger.warning("[DISCOVERER] Link scoring failed: {}", e)
        # Fallback: keyword match
        q_words = set(query.lower().split())
        scored = []
        for text, url in links:
            overlap = len(q_words & set(text.lower().split()))
            scored.append((text, url, overlap / max(len(q_words), 1)))
        return sorted(scored, key=lambda x: x[2], reverse=True)


def _wait_for_content(page: Page, timeout_ms: int = 10_000) -> None:
    """
    Wait for actual data content, not just the SPA shell.

    Primary: networkidle — blocks until all API/XHR calls finish (500ms quiet).
    Fallback: wait for known data elements (stat cards, tables).
    Final fallback: fixed 4s sleep.

    Note: never use generic selectors like 'main' or '#root > * > *' here —
    those fire on the empty SPA shell before any API data loads.
    """
    # Primary: wait for network idle (all data API calls complete)
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
        page.wait_for_timeout(400)
        logger.debug("[DISCOVERER] Network idle — page data should be loaded")
        return
    except Exception:
        pass

    # Fallback: wait for meaningful data elements (not shell containers)
    _DATA_SIGNALS = [
        ".ant-statistic", ".ant-table-tbody tr",
        ".ant-card .ant-card-body",
        "[class*='stat-card']", "[class*='statistic']",
        "[class*='card'] [class*='value']",
    ]
    try:
        page.wait_for_selector(", ".join(_DATA_SIGNALS), timeout=timeout_ms, state="visible")
        page.wait_for_timeout(600)
        logger.debug("[DISCOVERER] Data element signal received")
        return
    except Exception:
        pass

    # Final fallback
    logger.debug("[DISCOVERER] No content signal — waiting 4s")
    page.wait_for_timeout(4_000)


def _extract_month_target(query: str) -> datetime | None:
    """
    Parse a month/period hint from the query.
    Returns datetime(year, month, 1) or None if no hint found.
    """
    now = datetime.now()
    q = query.lower()

    # "last month" / "previous month"
    if re.search(r'\b(last|prev\w*)\s+month\b', q):
        first_this = now.replace(day=1)
        return (first_this - timedelta(days=1)).replace(day=1)

    # Named month
    _MONTHS = {
        "january": 1, "jan": 1, "february": 2, "feb": 2,
        "march": 3, "mar": 3, "april": 4, "apr": 4,
        "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
        "august": 8, "aug": 8, "september": 9, "sep": 9,
        "october": 10, "oct": 10, "november": 11, "nov": 11,
        "december": 12, "dec": 12,
    }
    for name, num in _MONTHS.items():
        if re.search(r'\b' + name + r'\b', q):
            year = now.year
            if num > now.month:
                year -= 1   # named month in the future → assume last year
            if year == now.year and num == now.month:
                return None  # asking about current month — no navigation needed
            return datetime(year, num, 1)

    return None


def _navigate_to_target_month(page: Page, target: datetime) -> bool:
    """
    Click the 'previous month' arrow until the page shows the target month.
    Returns True when the right month is visible (or was already visible).
    Works for any UI that renders a visible "Month YYYY" text with a prev arrow.
    """
    _MONTH_RE = re.compile(
        r'\b(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+(\d{4})\b', re.I
    )

    def displayed_month() -> datetime | None:
        try:
            text = page.evaluate("() => document.body.innerText")
            m = _MONTH_RE.search(text or "")
            if m:
                return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%B %Y")
        except Exception:
            pass
        return None

    def already_there() -> bool:
        dm = displayed_month()
        return dm is not None and dm.year == target.year and dm.month == target.month

    # Log page state before attempting navigation — tells us if the widget even loaded
    dm_before = displayed_month()
    logger.info("[MONTH_NAV] Target={} | URL={} | Page shows={}",
                target.strftime("%B %Y"),
                page.url,
                dm_before.strftime("%B %Y") if dm_before else "NO MONTH FOUND IN PAGE")
    if dm_before is None:
        try:
            # Grab a small snippet of visible text to diagnose what the page actually shows
            snippet = page.evaluate("() => document.body.innerText.slice(0, 600)")
            logger.debug("[MONTH_NAV] Page text sample: {!r}", snippet)
        except Exception:
            pass

    if already_there():
        return True

    # JS that finds and clicks the leftmost/prev navigation button near a month header.
    # On failure it returns a JSON diagnostic string (not null) so we can log exactly
    # what buttons and containers the page had at that moment.
    _CLICK_PREV_JS = """() => {
        // Prefer buttons with explicit prev semantics
        const selectors = [
            'button[aria-label*="prev" i]', 'button[aria-label*="previous" i]',
            'button[aria-label*="back" i]', 'button[aria-label*="last" i]',
            '[class*="prev"] button', '[class*="prev-btn"]', '[class*="prevBtn"]',
            '[class*="left-arrow"]', '[class*="arrowLeft"]',
        ];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el) { el.click(); return 'selector:' + sel; }
        }

        // Fallback: small buttons whose text is a left-pointing symbol
        for (const btn of document.querySelectorAll('button')) {
            const t = (btn.innerText || '').trim();
            if (t === '<' || t === '‹' || t === '←' || t === '❮' || t === '«') {
                btn.click();
                return 'symbol:' + t;
            }
        }

        // Last resort: the first narrow button inside any element named header/nav/calendar
        const containers = document.querySelectorAll(
            '[class*="header"], [class*="nav"], [class*="calendar"], [class*="picker"]'
        );
        for (const container of containers) {
            const btns = Array.from(container.querySelectorAll('button'));
            if (btns.length >= 2) {
                const sorted = btns
                    .filter(b => b.getBoundingClientRect().width > 0)
                    .sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
                if (sorted.length) { sorted[0].click(); return 'leftmost'; }
            }
        }

        // Nothing found — return diagnostic so Python can log what was actually on the page
        const allBtns = Array.from(document.querySelectorAll('button'))
            .map(b => (b.getAttribute('aria-label') || b.innerText || '').trim().slice(0, 40))
            .filter(t => t.length > 0)
            .slice(0, 10);
        const navCls = Array.from(document.querySelectorAll('[class*="header"],[class*="nav"],[class*="calendar"],[class*="picker"]'))
            .map(el => el.className.slice(0, 60))
            .slice(0, 5);
        return 'DIAGNOSTIC:' + JSON.stringify({
            total_buttons: document.querySelectorAll('button').length,
            aria_prev_count: document.querySelectorAll('[aria-label*="prev" i],[aria-label*="previous" i]').length,
            class_prev_count: document.querySelectorAll('[class*="prev"],[class*="Prev"]').length,
            button_labels: allBtns,
            nav_container_classes: navCls
        });
    }"""

    for _attempt in range(13):   # max 13 hops = just over 1 year back
        if already_there():
            return True
        try:
            result = page.evaluate(_CLICK_PREV_JS)
            if not result:
                logger.warning("[MONTH_NAV] JS returned null (attempt {}) — page may still be loading",
                               _attempt + 1)
                break
            if isinstance(result, str) and result.startswith("DIAGNOSTIC:"):
                diag = _json.loads(result[len("DIAGNOSTIC:"):])
                logger.warning(
                    "[MONTH_NAV] No prev button found (attempt {}) | "
                    "total_buttons={} aria_prev={} class_prev={} | "
                    "button_labels={} | nav_containers={}",
                    _attempt + 1,
                    diag.get("total_buttons", "?"),
                    diag.get("aria_prev_count", "?"),
                    diag.get("class_prev_count", "?"),
                    diag.get("button_labels", []),
                    diag.get("nav_container_classes", []),
                )
                break
            logger.debug("[MONTH_NAV] Prev-month click via {!r} (attempt {})", result, _attempt + 1)
            page.wait_for_timeout(700)
        except Exception as e:
            logger.debug("[MONTH_NAV] Month nav click failed: {}", e)
            break

    return already_there()


def _get_direct_url_hint(query: str, site_id: str, base_url: str) -> str | None:
    """
    Check registered page actions for a direct URL hint.
    Actions with a "keywords" list and a "url" path let discovery skip home-page
    navigation entirely and go straight to the right sub-page.
    """
    try:
        from tools.web_engine.store import get_all_actions_for_site
        from rapidfuzz import fuzz as _fuzz
        actions = get_all_actions_for_site(site_id)
        q_lower = query.lower()
        q_words = [w for w in re.findall(r'\w+', q_lower) if len(w) >= 5]
        base = base_url.rstrip("/")
        for action in actions:
            cfg = action["config"]
            keywords = cfg.get("keywords", [])
            url_path = cfg.get("url", "")
            if not url_path or not keywords:
                continue

            # Exact substring match
            if any(kw in q_lower for kw in keywords):
                full_url = (base + url_path) if url_path.startswith("/") else url_path
                logger.info("[DISCOVERER] Direct URL hint: {!r} → {!r}",
                            action["type"], full_url)
                return full_url

            # Fuzzy word match — catches STT garbling like "punctually" ≈ "punctuality"
            for kw in keywords:
                for kw_word in re.findall(r'\w+', kw):
                    if len(kw_word) < 5:
                        continue
                    for q_word in q_words:
                        if _fuzz.ratio(q_word, kw_word) >= 82:
                            full_url = (base + url_path) if url_path.startswith("/") else url_path
                            logger.info("[DISCOVERER] Direct URL hint (fuzzy {!r}≈{!r}): {!r} → {!r}",
                                        q_word, kw_word, action["type"], full_url)
                            return full_url
    except Exception as e:
        logger.debug("[DISCOVERER] Direct URL hint lookup failed: {}", e)
    return None


def _find_known_page_url(query: str, site_id: str) -> str | None:
    """
    Check stored pages for this site. If a page name fuzzy-matches the query
    well enough, return its URL so discovery starts there instead of home.
    """
    pages = store.get_pages_for_site(site_id)
    if not pages:
        return None
    try:
        from rapidfuzz import fuzz, process as fuzz_process
        names = [p["name"] for p in pages]
        match = fuzz_process.extractOne(
            query.lower(), [n.lower() for n in names],
            scorer=fuzz.token_set_ratio,
            score_cutoff=50,
        )
        if match:
            _, score, idx = match
            url = pages[idx]["url"]
            logger.info("[DISCOVERER] Known page match: {!r} (score={}) → {!r}", names[idx], score, url)
            return url
    except Exception as e:
        logger.debug("[DISCOVERER] Known page lookup failed: {}", e)
    return None


def _url_to_page_name(url: str) -> str | None:
    """Map a portal page URL to a logical page name used for API endpoint storage."""
    if "/my-activity" in url:
        return "activity"
    if "/leave" in url:
        return "leave"
    if "/requests" in url:
        return "requests"
    if "/work-journal" in url:
        return "work_journal"
    return None


# Endpoints under these paths are auth/infra — not data APIs worth saving
_SKIP_API_PATHS = ("/auth/", "/login/", "/token", "/refresh", "/logout",
                   "/healthz", "/metrics", "/static/", "/favicon")


_AUTH_RESPONSE_KEYS = {"access_token", "refresh_token", "token", "jwt_expiry", "bearer_token"}


def _save_captured_endpoints(site_id: str, page_url: str, captured: dict) -> None:
    """Persist intercepted JSON API calls to the api_endpoints table."""
    page_name = _url_to_page_name(page_url)
    logger.debug("[DISCOVERER] _save_captured_endpoints: page_url={!r} page_name={!r} captured={}",
                 page_url, page_name, len(captured))

    if not page_name:
        logger.debug("[DISCOVERER] Skip save — URL {!r} does not map to a known page_name", page_url)
        return
    if not captured:
        logger.debug("[DISCOVERER] Skip save — no API calls were intercepted")
        return

    saved = 0
    skipped_auth = 0
    skipped_small = 0
    skipped_path = 0

    for raw_url, captured_info in captured.items():
        # Handle both new format (dict with data/method/body) and old format (raw data)
        if isinstance(captured_info, dict) and "data" in captured_info:
            data   = captured_info["data"]
            method = captured_info.get("method", "GET")
            body   = captured_info.get("body")
        else:
            data   = captured_info
            method = "GET"
            body   = None

        # Skip responses with too few fields — likely not data endpoints
        if not isinstance(data, dict) or len(data) < 2:
            skipped_small += 1
            logger.debug("[DISCOVERER] Skip {!r} — response has <2 fields (type={}, len={})",
                         raw_url, type(data).__name__, len(data) if isinstance(data, dict) else "n/a")
            continue

        # Skip known infrastructure paths
        if any(skip in raw_url for skip in _SKIP_API_PATHS):
            skipped_path += 1
            logger.debug("[DISCOVERER] Skip {!r} — matches _SKIP_API_PATHS", raw_url)
            continue

        # Skip auth/session endpoints — they return JWT tokens, not HR data
        nested = data.get("result", data)
        if isinstance(nested, dict) and _AUTH_RESPONSE_KEYS & nested.keys():
            skipped_auth += 1
            found_keys = _AUTH_RESPONSE_KEYS & nested.keys()
            logger.debug("[DISCOVERER] Skip {!r} — auth response keys found: {}", raw_url, found_keys)
            continue

        # Strip query params — month/page params are injected dynamically at call time
        parsed = urlparse(raw_url)
        clean_url = urlunparse(parsed._replace(query="", fragment=""))

        logger.debug("[DISCOVERER] Saving endpoint: method={} url={} body={}",
                     method, clean_url, repr(body[:100]) if body else "none")
        store.save_api_endpoint(site_id, page_name, clean_url, method,
                                _json.dumps(data), request_body=body)
        saved += 1

    logger.info("[DISCOVERER] Endpoint save summary: saved={} skipped_auth={} "
                "skipped_small={} skipped_path={} (page={!r})",
                saved, skipped_auth, skipped_small, skipped_path, page_name)


def _start_background_enrichment(site_id: str, page_id: str,
                                  url: str, session: dict | None) -> None:
    """Spawn a daemon thread to click all tabs and extract their content.
    No-ops if enrichment for this URL is already in progress."""
    if url in _enriching:
        logger.debug("[DISCOVERER] Enrichment already running for {!r} — skipping", url)
        return
    _enriching.add(url)
    t = threading.Thread(
        target=_background_enrich,
        args=(site_id, page_id, url, session),
        daemon=True,
    )
    t.start()
    logger.debug("[DISCOVERER] Background enrichment thread started for {!r}", url)


def _background_enrich(site_id: str, page_id: str, url: str,
                        session: dict | None) -> None:
    """Background: click each tab, extract, store. Releases the _enriching lock on exit."""
    ctx_kwargs = {"storage_state": session} if session else {}
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(**ctx_kwargs)
        page    = context.new_page()
        page.goto(url, timeout=35_000, wait_until="domcontentloaded")
        _wait_for_content(page)

        # Find all tabs
        tabs = page.query_selector_all(
            ".ant-tabs-tab, [role='tab'], [class*='tab-item']"
        )
        logger.info("[ENRICHER] Found {} tabs on {!r}", len(tabs), url)

        for tab in tabs:
            try:
                tab_text = tab.inner_text().strip()
                tab.click()
                page.wait_for_timeout(1_500)
                tab_url = page.url
                tab_page_id = store.upsert_page(site_id, tab_url, tab_text)
                sections = extract_page(page, site_id, tab_page_id, tab_url)
                for sec in sections:
                    from tools.web_engine import store as s
                    sect_id = s.upsert_section(
                        tab_page_id, sec["label"], sec.get("selector", "")
                    )
                    s.set_cache(sect_id, sec["value"])
                logger.info("[ENRICHER] Tab {!r}: {} sections stored", tab_text, len(sections))
            except Exception as e:
                logger.debug("[ENRICHER] Tab error: {}", e)

        browser.close()
        pw.stop()
        logger.info("[ENRICHER] Background enrichment complete for {!r}", url)
    except Exception as e:
        logger.warning("[ENRICHER] Background enrichment failed: {}", e)
    finally:
        _enriching.discard(url)
