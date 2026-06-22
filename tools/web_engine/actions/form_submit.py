"""
FormSubmitAction: fills and submits a form using config stored in SQLite.

Primary path:  browser extension (user's real browser, already authenticated).
Fallback path: Playwright visible browser (uses saved session).

Action config format:
{
  "url":    "/my-apps/work-journal",
  "fields": [{"selector": "textarea", "value": "{text}"}],
  "submit": "button[type='submit']",
  "wait_ms": 2000
}
{key} placeholders in field values are replaced with runtime substitutions.
"""
from loguru import logger

from tools.web_engine import store
from tools.web_engine.actions.navigate import open_page, close_page

_DEFAULT_WAIT_MS = 2_000


def run(site_id: str, action_type: str, substitutions: dict, recorder=None) -> str:
    """
    Loads the action config, fills the form, and submits it.
    Tries the browser extension first; falls back to Playwright on any failure.
    """
    logger.info("[FormSubmit] ── run: site={!r} action_type={!r} subs={}", site_id, action_type, substitutions)

    config = store.get_action(site_id, action_type)
    logger.debug("[FormSubmit] action config: {}", config)

    if not config:
        logger.error("[FormSubmit] No config found for action_type={!r} site={!r}", action_type, site_id)
        return f"No action config found for {action_type!r} on {site_id}."

    # ── Primary: extension ────────────────────────────────────────────────
    from tools.browser_extension import connection_manager
    ext_connected = connection_manager.is_connected()
    logger.info("[FormSubmit] extension connected: {}", ext_connected)

    if ext_connected:
        logger.info("[FormSubmit] PATH → Extension (primary)")
        try:
            return _run_via_extension(site_id, action_type, config, substitutions, recorder)
        except Exception as exc:
            logger.warning("[FormSubmit] Extension path FAILED: {} — {}", type(exc).__name__, exc)
            logger.info("[FormSubmit] Falling back to Playwright")
    else:
        logger.info("[FormSubmit] Extension not connected → going straight to Playwright")

    # ── Fallback: Playwright ──────────────────────────────────────────────
    logger.info("[FormSubmit] PATH → Playwright (fallback)")
    return _fallback_playwright(site_id, action_type, config, substitutions, recorder)


# ── Extension path ────────────────────────────────────────────────────────────

def _run_via_extension(site_id: str, action_type: str, config: dict, substitutions: dict, recorder=None) -> str:
    from tools.browser_extension import run_command_sync

    site     = store.get_site(site_id)
    base_url = site["base_url"] if site else f"https://{site_id}"
    url      = base_url.rstrip("/") + "/" + config["url"].lstrip("/")
    logger.debug("[FormSubmit.ext] Full URL to navigate: {!r}", url)

    # Navigate
    logger.debug("[FormSubmit.ext] Sending navigate command …")
    nav = run_command_sync("navigate", {"url": url})
    logger.info("[FormSubmit.ext] navigate result: status={!r} data={}", nav.get("status"), nav.get("data"))

    if nav.get("status") != "ok":
        raise RuntimeError(f"navigate failed: {nav.get('error')}")

    if recorder:
        recorder.step("web", "navigate", {"url": url}, narration=f"Opening {url}")

    # Build substituted fields
    fields = [
        {"selector": f["selector"], "value": _substitute(f["value"], substitutions)}
        for f in config.get("fields", [])
    ]
    logger.info("[FormSubmit.ext] Fields to fill ({}): {}", len(fields), fields)

    submit_sel = config.get("submit")
    wait_ms    = config.get("wait_ms", _DEFAULT_WAIT_MS)
    logger.debug("[FormSubmit.ext] submit_selector={!r} wait_ms={}", submit_sel, wait_ms)

    # Fill form
    logger.debug("[FormSubmit.ext] Sending fill_form command …")
    fill = run_command_sync("fill_form", {
        "fields":  fields,
        "submit":  submit_sel,
        "wait_ms": wait_ms,
    })
    logger.info("[FormSubmit.ext] fill_form result: status={!r} data={}", fill.get("status"), fill.get("data"))

    if fill.get("status") != "ok":
        raise RuntimeError(f"fill_form failed: {fill.get('error')}")

    data      = fill.get("data", {})
    submitted = data.get("submitted", False)
    fields_ok = [f for f in data.get("fields", []) if f.get("ok")]
    fields_err = [f for f in data.get("fields", []) if not f.get("ok")]

    logger.info("[FormSubmit.ext] Fields filled OK: {} | Errors: {} | Submitted: {}",
                len(fields_ok), len(fields_err), submitted)
    if fields_err:
        logger.warning("[FormSubmit.ext] Field errors: {}", fields_err)
    if not submitted:
        logger.warning("[FormSubmit.ext] Form filled but NOT submitted (submit button not found or not clicked)")

    if recorder:
        recorder.step("web", action_type, substitutions, narration="Filled and submitted the form")
        recorder.checkpoint()
        logger.debug("[FormSubmit.ext] Procedure step recorded + checkpoint")

    preview = substitutions.get("text", "")
    preview = preview[:60] + "..." if len(preview) > 60 else preview
    logger.info("[FormSubmit.ext] ── SUCCESS: submitted={!r}", preview)
    return f'Done. Submitted: "{preview}"'


# ── Playwright fallback ───────────────────────────────────────────────────────

def _fallback_playwright(site_id: str, action_type: str, config: dict, substitutions: dict, recorder=None) -> str:
    site     = store.get_site(site_id)
    base_url = site["base_url"] if site else f"https://{site_id}"
    url      = base_url.rstrip("/") + "/" + config["url"].lstrip("/")
    logger.debug("[FormSubmit.pw] Full URL: {!r}", url)

    if recorder:
        recorder.step("web", "navigate", {"url": url}, narration=f"Opening {url}")

    logger.debug("[FormSubmit.pw] Opening visible Playwright browser …")
    result = open_page(site_id, url, headless=False)
    if not result:
        logger.error("[FormSubmit.pw] open_page returned None — cannot proceed")
        return "Could not open the browser for this action."

    pw, browser, context, page = result
    logger.info("[FormSubmit.pw] Browser open — current URL: {!r}", page.url)

    try:
        wait_ms = config.get("wait_ms", _DEFAULT_WAIT_MS)
        logger.debug("[FormSubmit.pw] Waiting {}ms for page to settle …", wait_ms)
        page.wait_for_timeout(wait_ms)

        fields = config.get("fields", [])
        logger.info("[FormSubmit.pw] Filling {} field(s) …", len(fields))

        for i, field in enumerate(fields):
            selector = field["selector"]
            value    = _substitute(field["value"], substitutions)
            logger.debug("[FormSubmit.pw] Field {}/{}: selector={!r} value_len={}", i + 1, len(fields), selector, len(value))

            el = page.query_selector(selector)
            if not el:
                logger.debug("[FormSubmit.pw] Primary selector {!r} not found — trying contenteditable fallback", selector)
                el = page.query_selector("[contenteditable='true']")

            if el:
                el.click()
                page.keyboard.press("Control+a")
                if selector in ("textarea",) or "input" in selector:
                    el.fill(value)
                else:
                    page.keyboard.type(value)
                logger.info("[FormSubmit.pw] Filled {!r} with {} chars", selector, len(value))
            else:
                logger.warning("[FormSubmit.pw] Field {!r} NOT found on page", selector)

        submit_sel = config.get("submit")
        logger.debug("[FormSubmit.pw] Looking for submit button: {!r}", submit_sel)

        if submit_sel:
            btn = page.query_selector(submit_sel)
            if not btn:
                logger.debug("[FormSubmit.pw] Primary submit {!r} not found — trying .ant-btn-primary fallback", submit_sel)
                btn = page.query_selector(".ant-btn-primary")

            if btn:
                btn.click()
                logger.info("[FormSubmit.pw] Submit button clicked — waiting {}ms", _DEFAULT_WAIT_MS)
                page.wait_for_timeout(_DEFAULT_WAIT_MS)

                if recorder:
                    recorder.step("web", action_type, substitutions, narration="Filled and submitted the form")
                    recorder.checkpoint()

                preview = substitutions.get("text", "")
                preview = preview[:60] + "..." if len(preview) > 60 else preview
                logger.info("[FormSubmit.pw] ── SUCCESS: submitted={!r}", preview)
                return f'Done. Submitted: "{preview}"'
            else:
                logger.error("[FormSubmit.pw] Submit button NOT found for selector={!r}", submit_sel)
                return "Form filled but submit button not found."

        logger.info("[FormSubmit.pw] No submit selector — returning 'Form filled'")
        return "Form filled."

    except Exception as exc:
        logger.exception("[FormSubmit.pw] Exception during form submit: {}", exc)
        return f"Form submission error: {exc}"
    finally:
        logger.debug("[FormSubmit.pw] Closing browser …")
        page.wait_for_timeout(1_500)
        close_page(pw, browser)


# ── Utilities ─────────────────────────────────────────────────────────────────

def _substitute(template: str, subs: dict) -> str:
    for key, val in subs.items():
        template = template.replace(f"{{{key}}}", val)
    return template


def action_type_from_config(config: dict) -> str:
    return config.get("url", "form_submit").lstrip("/").replace("/", "_")
