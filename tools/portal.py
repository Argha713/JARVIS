"""
Portal tool — reads and interacts with people.codeclouds.com.
Uses a saved Playwright session (data/portal_session.json).
If session has expired, returns a prompt to run the refresh_session action.
"""
import json
import re
import time
from pathlib import Path

from loguru import logger
from playwright.sync_api import sync_playwright, Page

BASE_URL      = "https://people.codeclouds.com"
SESSION_FILE  = "data/portal_session.json"
PATHS_FILE    = "data/portal_paths.json"

DEFAULT_PATHS = {
    "activity": "/my-activity",
    "leave":    "/leave",
    "requests": "/requests",
    "eod":      "/my-apps/work-journal",
    "booking":  "/my-apps/booking-system",
}

LOGIN_TIMEOUT_MS = 120_000   # 2 min to log in manually


class Portal:
    def __init__(self, narration, config: dict):
        self.narration = narration
        self._paths    = self._load_paths()

    # ──────────────────────────────────────────────
    # Public interface
    # ──────────────────────────────────────────────

    def run(self, params: dict) -> str:
        action = params.get("action", "")
        dispatch = {
            "get_activity":    self._get_activity,
            "check_leave":     self._check_leave,
            "check_requests":  self._check_requests,
            "submit_eod":      lambda: self._submit_eod(params.get("text", "")),
            "book_seat":       self._book_seat,
            "refresh_session": self._refresh_session,
        }
        fn = dispatch.get(action)
        if not fn:
            return (
                f"Unknown portal action: {action!r}. "
                f"Available: {', '.join(dispatch)}"
            )
        try:
            return fn()
        except Exception as e:
            logger.exception(f"[PORTAL] {action} failed")
            return f"Portal error: {e}"

    # ──────────────────────────────────────────────
    # Actions
    # ──────────────────────────────────────────────

    def _get_activity(self) -> str:
        self.narration.step("Checking activity on portal...")
        return self._with_page(
            self._paths["activity"],
            self._extract_activity,
            headless=True,
        )

    def _check_leave(self) -> str:
        self.narration.step("Checking leave balances...")
        return self._with_page(
            self._paths["leave"],
            self._extract_leave,
            headless=True,
        )

    def _check_requests(self) -> str:
        self.narration.step("Checking support requests...")
        return self._with_page(
            self._paths["requests"],
            self._extract_requests,
            headless=True,
        )

    def _submit_eod(self, text: str) -> str:
        if not text:
            return "Please tell me what to write in the EOD report."
        self.narration.step("Opening work journal...")
        return self._with_page(
            self._paths["eod"],
            lambda page: self._fill_eod(page, text),
            headless=False,  # visible so user can see it happen
            save_session=True,
        )

    def _book_seat(self) -> str:
        self.narration.step("Opening seat booking...")
        return self._with_page(
            self._paths["booking"],
            self._extract_booking,
            headless=False,
        )

    def _refresh_session(self) -> str:
        """Open a visible browser and wait for the user to log in manually."""
        logger.info("[PORTAL] refresh_session: opening visible browser")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False, slow_mo=100)
                context = browser.new_context()
                page    = context.new_page()

                # Navigate to portal — this will redirect to Google login if session expired
                try:
                    page.goto(BASE_URL + "/home", timeout=30000, wait_until="domcontentloaded")
                except Exception:
                    pass  # timeout on redirect is fine — page may still be loading

                # Wait 2s for any redirect to settle
                page.wait_for_timeout(2000)
                logger.info(f"[PORTAL] refresh_session | URL after settle: {page.url!r}")

                expired, reason = self._is_expired(page)
                logger.info(f"[PORTAL] refresh_session | expired={expired} reason={reason!r}")

                if expired:
                    # Redirected to portal login or Google — need user to log in
                    logger.info("[PORTAL] Login required, waiting for user to complete auth...")
                    self.narration.say(
                        "The browser is open, sir. Please log in and I'll wait right here."
                    )
                    try:
                        # Wait until we land back on the portal domain (login complete)
                        page.wait_for_url(
                            lambda url: "people.codeclouds.com" in url and
                                        "sign" not in url.lower() and
                                        "login" not in url.lower(),
                            timeout=LOGIN_TIMEOUT_MS,
                        )
                        page.wait_for_timeout(2000)
                        logger.info(f"[PORTAL] Login complete | URL: {page.url!r}")
                    except Exception:
                        browser.close()
                        return "Login timed out. Please try again."
                else:
                    # Session still valid — just refresh the saved cookies
                    logger.info("[PORTAL] Session still valid, re-saving cookies.")
                    self.narration.say("Portal session is still active. Refreshing saved cookies.")

                Path(SESSION_FILE).parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=SESSION_FILE)
                browser.close()

            logger.info(f"[PORTAL] Session refreshed → {SESSION_FILE}")
            return "Portal session refreshed. You're logged in."
        except Exception as e:
            logger.exception("[PORTAL] refresh_session failed")
            return f"Failed to refresh session: {e}"

    # ──────────────────────────────────────────────
    # Page helpers
    # ──────────────────────────────────────────────

    def _with_page(self, path: str, fn, *, headless: bool = True,
                   save_session: bool = False, _retried: bool = False) -> str:
        url = BASE_URL + path
        t0  = time.perf_counter()
        session_exists = Path(SESSION_FILE).exists()
        logger.info(f"[PORTAL] ── _with_page {'(retry)' if _retried else ''} ───────────────────")
        logger.info(f"[PORTAL] Target URL  : {url}")
        logger.info(f"[PORTAL] Headless    : {headless}")
        logger.info(f"[PORTAL] Session file: {'found' if session_exists else 'MISSING'} ({SESSION_FILE})")

        # Track outcome outside the with-block so we can call _handle_expiry
        # AFTER sync_playwright() fully exits (nested contexts cause a crash).
        _expired        = False
        _early_return   = None   # set to a string to return early without expiry handling
        result          = None

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless, slow_mo=50)
            ctx_kwargs: dict = {}
            if session_exists:
                ctx_kwargs["storage_state"] = SESSION_FILE
                logger.debug("[PORTAL] Loading saved session cookies")
            else:
                logger.warning("[PORTAL] No session file — browser will start unauthenticated")
            context = browser.new_context(**ctx_kwargs)
            page    = context.new_page()

            logger.debug(f"[PORTAL] Navigating (timeout=35s, wait=domcontentloaded)...")
            t_nav = time.perf_counter()
            try:
                page.goto(url, timeout=35000, wait_until="domcontentloaded")
                logger.info(f"[PORTAL] Navigation done in {time.perf_counter()-t_nav:.1f}s | landed on: {page.url!r}")
            except Exception as e:
                err = str(e)
                logger.warning(f"[PORTAL] page.goto raised: {err[:200]!r}")
                logger.debug(f"[PORTAL] URL at exception: {page.url!r}")

                expired_check, reason = self._is_expired(page)
                if expired_check:
                    logger.warning(f"[PORTAL] Session expired (goto error). Reason: {reason}")
                    browser.close()
                    _expired = True
                else:
                    _NET_ERRORS = (
                        "ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION_REFUSED",
                        "ERR_INTERNET_DISCONNECTED", "ERR_NETWORK_CHANGED",
                        "ERR_CONNECTION_TIMED_OUT", "ERR_CONNECTION_RESET",
                    )
                    if any(code in err for code in _NET_ERRORS):
                        logger.error(f"[PORTAL] Network error: {err[:100]!r}")
                        browser.close()
                        _early_return = "I can't reach the portal. Please check your internet connection and try again."
                    elif "Timeout" in err or "timeout" in err:
                        logger.warning("[PORTAL] Timeout — portal too slow to respond")
                        browser.close()
                        _early_return = "The portal took too long to respond. Please try again in a moment."
                    else:
                        logger.error(f"[PORTAL] Unhandled goto error: {err}")
                        browser.close()
                        _early_return = f"Could not load portal page: {e}"

            if not _expired and _early_return is None:
                logger.debug(f"[PORTAL] Final URL after navigation: {page.url!r}")
                expired_check, reason = self._is_expired(page)
                if expired_check:
                    logger.warning(f"[PORTAL] Session expired (post-navigation). Reason: {reason}")
                    browser.close()
                    _expired = True
                else:
                    logger.info(f"[PORTAL] Session OK — running extractor: {fn.__name__ if hasattr(fn, '__name__') else str(fn)}")
                    result = fn(page)

                    if save_session:
                        logger.debug("[PORTAL] Saving session cookies after action")
                        context.storage_state(path=SESSION_FILE)

                    if not headless:
                        page.wait_for_timeout(2000)

                    browser.close()

        # sync_playwright() has fully exited — safe to open a new playwright context now

        if _early_return is not None:
            return _early_return

        if _expired:
            return self._handle_expiry(path, fn, headless, save_session, _retried)

        elapsed = time.perf_counter() - t0
        logger.info(f"[PORTAL] {path} completed in {elapsed:.1f}s")
        return result

    def _handle_expiry(self, path: str, fn, headless: bool,
                       save_session: bool, already_retried: bool) -> str:
        """Called whenever session expiry is detected. Auto-refreshes once, then retries."""
        if already_retried:
            logger.error("[PORTAL] Session still expired after refresh — giving up")
            return "Portal session refresh failed. Please try again later."

        logger.info("[PORTAL] Auto-refreshing session and retrying...")
        self.narration.say("Looks like you've been logged out of the portal, sir. Opening the browser — please log in and I'll carry on.")
        refresh = self._refresh_session()
        logger.info(f"[PORTAL] Refresh result: {refresh!r}")

        if "refreshed" not in refresh.lower() and "logged in" not in refresh.lower():
            return f"Could not refresh portal session: {refresh}"

        # Retry the original request with the new session
        logger.info("[PORTAL] Session refreshed — retrying original request...")
        self.narration.say("You're back in, sir. Give me a moment.")
        return self._with_page(path, fn, headless=headless,
                               save_session=save_session, _retried=True)

    @staticmethod
    def _is_expired(page: Page) -> tuple[bool, str]:
        """
        Returns (is_expired, reason_string).
        Checks URL patterns AND page content so it catches both Google OAuth
        redirects and the portal's own BeSimplified sign-in page.
        """
        url = page.url
        logger.debug(f"[PORTAL] _is_expired check | url={url!r}")

        # URL-based checks
        url_lower = url.lower()
        _AUTH_URL_PATTERNS = (
            "accounts.google.com",  # Google OAuth
            "/login",               # generic login paths
            "/sign-in",
            "/signin",
            "/logout",              # portal logs out expired session then redirects to login
            "/auth/",
        )
        for pattern in _AUTH_URL_PATTERNS:
            if pattern in url_lower:
                return True, f"auth URL pattern {pattern!r} matched ({url})"

        # Content-based check — catches portal's own login page (e.g. BeSimplified welcome screen)
        try:
            result = page.evaluate("""() => {
                const body = (document.body?.innerText || '').toLowerCase();
                const hasSignInText  = body.includes('sign in') || body.includes('log in') || body.includes('sign into');
                const hasPasswordBox = !!document.querySelector('input[type="password"]');
                const hasSubmitBtn   = !!document.querySelector('button[type="submit"]');
                return {
                    hasSignInText,
                    hasPasswordBox,
                    hasSubmitBtn,
                    bodyPreview: document.body?.innerText?.slice(0, 150) || ''
                };
            }""")
            logger.debug(
                f"[PORTAL] Login page content check: "
                f"signInText={result['hasSignInText']}, "
                f"passwordBox={result['hasPasswordBox']}, "
                f"submitBtn={result['hasSubmitBtn']} | "
                f"preview={result['bodyPreview'][:80]!r}"
            )
            if result["hasPasswordBox"] or (result["hasSignInText"] and result["hasSubmitBtn"]):
                return True, "login page content detected (password input or sign-in form present)"
        except Exception as e:
            err = str(e)
            # "Execution context was destroyed" means the page is mid-navigation (redirecting away)
            # — almost certainly an auth redirect, treat as expired
            if "context was destroyed" in err.lower() or "navigation" in err.lower():
                return True, f"page mid-navigation during content check — likely auth redirect ({err[:80]})"
            logger.debug(f"[PORTAL] _is_expired content check failed: {e}")

        return False, "ok"

    # ──────────────────────────────────────────────
    # Extractors
    # ──────────────────────────────────────────────

    @staticmethod
    def _extract_activity(page: Page) -> str:
        logger.info(f"[PORTAL] _extract_activity | url={page.url!r}")

        # Log DOM state upfront so we always know what the page contains
        try:
            dom = page.evaluate("""() => ({
                statCard:    document.querySelectorAll('.stat-card').length,
                antStat:     document.querySelectorAll('.ant-statistic').length,
                antStatVal:  document.querySelectorAll('.ant-statistic-content-value').length,
                antTabs:     document.querySelectorAll('.ant-tabs-tab').length,
                activeTab:   document.querySelector('.ant-tabs-tab-active')?.innerText?.trim() || 'none',
                bodyLen:     document.body?.innerText?.length || 0,
                bodyPreview: document.body?.innerText?.slice(0, 300) || '',
            })""")
            logger.debug(
                f"[PORTAL] DOM snapshot | "
                f"stat-card={dom['statCard']} ant-statistic={dom['antStat']} "
                f"ant-statistic-value={dom['antStatVal']} | "
                f"tabs={dom['antTabs']} active-tab={dom['activeTab']!r} | "
                f"body-len={dom['bodyLen']}"
            )
            logger.debug(f"[PORTAL] Page text preview: {dom['bodyPreview'][:200]!r}")
        except Exception as e:
            logger.warning(f"[PORTAL] DOM snapshot failed: {e}")
            dom = {}

        # Wait for whichever selector appears first — layout varies by time of day
        loaded = False
        for selector in (".stat-card", ".ant-statistic", ".ant-statistic-content-value"):
            logger.debug(f"[PORTAL] Waiting for selector {selector!r} (timeout=8s)...")
            try:
                page.wait_for_selector(selector, timeout=8000)
                loaded = True
                logger.info(f"[PORTAL] Activity page ready — matched selector {selector!r}")
                break
            except Exception as e:
                logger.debug(f"[PORTAL] Selector {selector!r} not found: {e.__class__.__name__}")
                continue

        if not loaded:
            debug_path = "data/portal_activity_debug.png"
            html_path  = "data/portal_activity_debug.html"
            try:
                page.screenshot(path=debug_path, full_page=True)
                page.content()  # force full HTML dump
                Path(html_path).write_text(page.content(), encoding="utf-8")
                logger.warning(
                    f"[PORTAL] All selectors failed. "
                    f"Screenshot → {debug_path} | HTML → {html_path}"
                )
            except Exception as e:
                logger.warning(f"[PORTAL] Debug dump failed: {e}")
            return (
                "I couldn't read the activity data. "
                "A debug screenshot was saved to data/portal_activity_debug.png — check what the portal is showing."
            )

        cards = page.evaluate("""() => {
            // Primary: custom stat-card wrapper (after-hours layout)
            const statCards = document.querySelectorAll('.stat-card');
            if (statCards.length > 0) {
                return Array.from(statCards)
                    .map(el => el.innerText.trim().replace(/\\s+/g, ' '))
                    .filter(Boolean);
            }

            // Fallback: read each ant-statistic + its sibling label text
            // Walk up to the nearest card/column container and grab the full text
            const seen = new Set();
            const results = [];
            document.querySelectorAll('.ant-statistic').forEach(stat => {
                // Find the closest ancestor that wraps both the number and the label
                let container = stat.parentElement;
                for (let i = 0; i < 5 && container; i++) {
                    const cls = container.className || '';
                    if (cls.includes('ant-col') || cls.includes('ant-card') || cls.includes('card')) break;
                    container = container.parentElement;
                }
                if (!container) container = stat.parentElement;
                const key = container.innerText.slice(0, 30);
                if (seen.has(key)) return;
                seen.add(key);
                const text = container.innerText.trim().replace(/\\s+/g, ' ');
                if (text) results.push(text);
            });
            return results;
        }""")

        logger.info(f"[PORTAL] activity: {len(cards)} stat entries found")
        if not cards:
            return "I couldn't read the activity data. The portal page may have changed."

        lines = ["Here are your activity stats for this month:"]
        for card in cards:
            clean = card.strip()
            if clean:
                lines.append(f"  {clean}")
        return "\n".join(lines)

    @staticmethod
    def _extract_leave(page: Page) -> str:
        try:
            page.wait_for_selector(".font-18", timeout=10000)
        except Exception:
            logger.warning("[PORTAL] leave balance selector not found in time")

        data = page.evaluate("""() => {
            const results = [];
            // Each leave card has a <p> with 'Leave' in it plus a balance fraction
            const allParagraphs = document.querySelectorAll('p, h4, h3');
            for (const p of allParagraphs) {
                const txt = (p.innerText || '').trim();
                if (!txt.includes('Leave') || txt.length > 50) continue;

                // Walk up to find the enclosing card column
                let card = p.parentElement;
                for (let i = 0; i < 6 && card; i++) {
                    if (card.classList.contains('ant-col') ||
                        card.classList.contains('ant-card')) break;
                    card = card.parentElement;
                }
                if (!card) continue;

                const raw = card.innerText.trim().replace(/\\s+/g, ' ');
                results.push(raw);
            }
            return results;
        }""")

        logger.info(f"[PORTAL] leave: {len(data)} cards")
        if not data:
            return "I couldn't read the leave balances."

        # Deduplicate
        seen   = set()
        lines  = ["Your leave balances:"]
        for item in data:
            clean = item.strip()
            if clean and clean not in seen and len(clean) < 150:
                seen.add(clean)
                lines.append(f"  {clean}")
        return "\n".join(lines)

    @staticmethod
    def _extract_requests(page: Page) -> str:
        try:
            page.wait_for_selector(".ant-table-tbody", timeout=10000)
        except Exception:
            logger.warning("[PORTAL] requests table not found in time")

        data = page.evaluate("""() => {
            const tbody = document.querySelector('.ant-table-tbody');
            if (!tbody) return [];
            return Array.from(tbody.querySelectorAll('tr'))
                .map(r => r.innerText.trim().replace(/\\s+/g, ' '))
                .filter(Boolean)
                .slice(0, 10);
        }""")

        if not data:
            return "No support requests found, or the page didn't load."
        lines = ["Your recent support requests:"]
        for row in data:
            lines.append(f"  {row}")
        return "\n".join(lines)

    @staticmethod
    def _fill_eod(page: Page, text: str) -> str:
        # Wait for the page to fully load
        page.wait_for_timeout(3000)

        # Try textarea first
        textarea = page.query_selector("textarea")
        if textarea:
            textarea.fill(text)
            logger.info("[PORTAL] Filled EOD via textarea")
        else:
            # Rich text / contenteditable editor
            editor = page.query_selector("[contenteditable='true']")
            if editor:
                editor.click()
                page.keyboard.press("Control+a")
                page.keyboard.type(text)
                logger.info("[PORTAL] Filled EOD via contenteditable")
            else:
                return "I couldn't find the EOD text editor on the page. The portal may have changed."

        # Find and click the submit button
        submit = page.query_selector("button[type='submit']") or \
                 page.query_selector(".ant-btn-primary")
        if submit:
            submit.click()
            page.wait_for_timeout(2000)
            preview = text[:60] + "..." if len(text) > 60 else text
            return f"EOD submitted: \"{preview}\""

        return "EOD text filled but I couldn't find the submit button. Please check the portal."

    @staticmethod
    def _extract_booking(page: Page) -> str:
        page.wait_for_timeout(3000)
        text = page.evaluate("""() => {
            document.querySelectorAll('script, style').forEach(e => e.remove());
            return document.body ? document.body.innerText.trim().replace(/\\s+/g, ' ') : '';
        }""")
        if len(text) > 500:
            text = text[:500] + "..."
        return f"Seat booking page loaded. Content preview: {text}"

    # ──────────────────────────────────────────────
    # Config helpers
    # ──────────────────────────────────────────────

    @staticmethod
    def _load_paths() -> dict:
        try:
            with open(PATHS_FILE, encoding="utf-8") as f:
                overrides = json.load(f)
            return {**DEFAULT_PATHS, **overrides}
        except FileNotFoundError:
            return dict(DEFAULT_PATHS)
