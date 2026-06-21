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
    config = store.get_action(site_id, action_type)
    if not config:
        return f"No action config found for {action_type!r} on {site_id}."

    # ── Primary: extension ────────────────────────────────────────────────
    from tools.browser_extension import connection_manager
    if connection_manager.is_connected():
        try:
            return _run_via_extension(site_id, config, substitutions, recorder)
        except Exception as exc:
            logger.warning("[FORM] Extension path failed ({}), falling back to Playwright", exc)

    # ── Fallback: Playwright ──────────────────────────────────────────────
    return _fallback_playwright(site_id, config, substitutions, recorder)


# ── Extension path ────────────────────────────────────────────────────────────

def _run_via_extension(site_id: str, config: dict, substitutions: dict, recorder=None) -> str:
    from tools.browser_extension import run_command_sync

    site     = store.get_site(site_id)
    base_url = site["base_url"] if site else f"https://{site_id}"
    url      = base_url.rstrip("/") + "/" + config["url"].lstrip("/")

    # Navigate (extension opens/reuses a background tab)
    nav = run_command_sync("navigate", {"url": url})
    if nav.get("status") != "ok":
        raise RuntimeError(f"navigate failed: {nav.get('error')}")

    if recorder:
        recorder.step("web", "navigate", {"url": url}, narration=f"Opening {url}")

    # Build fields with substitutions applied
    fields = [
        {"selector": f["selector"], "value": _substitute(f["value"], substitutions)}
        for f in config.get("fields", [])
    ]

    fill = run_command_sync("fill_form", {
        "fields":   fields,
        "submit":   config.get("submit"),
        "wait_ms":  config.get("wait_ms", _DEFAULT_WAIT_MS),
    })
    if fill.get("status") != "ok":
        raise RuntimeError(f"fill_form failed: {fill.get('error')}")

    data = fill.get("data", {})
    if not data.get("submitted"):
        logger.warning("[FORM] Extension: form filled but not submitted")

    if recorder:
        recorder.step("web", action_type_from_config(config), substitutions,
                      narration="Filled and submitted the form")
        recorder.checkpoint()

    preview = substitutions.get("text", "")
    preview = preview[:60] + "..." if len(preview) > 60 else preview
    logger.info("[FORM] Extension: submitted {!r}", preview)
    return f'Done. Submitted: "{preview}"'


# ── Playwright fallback ───────────────────────────────────────────────────────

def _fallback_playwright(site_id: str, config: dict, substitutions: dict, recorder=None) -> str:
    site     = store.get_site(site_id)
    base_url = site["base_url"] if site else f"https://{site_id}"
    url      = base_url.rstrip("/") + "/" + config["url"].lstrip("/")

    if recorder:
        recorder.step("web", "navigate", {"url": url}, narration=f"Opening {url}")

    result = open_page(site_id, url, headless=False)
    if not result:
        return "Could not open the browser for this action."

    pw, browser, context, page = result
    try:
        page.wait_for_timeout(config.get("wait_ms", _DEFAULT_WAIT_MS))

        for field in config.get("fields", []):
            selector = field["selector"]
            value    = _substitute(field["value"], substitutions)
            el = page.query_selector(selector) or page.query_selector("[contenteditable='true']")
            if el:
                el.click()
                page.keyboard.press("Control+a")
                if selector in ("textarea",) or "input" in selector:
                    el.fill(value)
                else:
                    page.keyboard.type(value)
                logger.info("[FORM] Playwright: filled {!r} ({} chars)", selector, len(value))
            else:
                logger.warning("[FORM] Playwright: field not found: {!r}", selector)

        submit_sel = config.get("submit")
        if submit_sel:
            btn = page.query_selector(submit_sel) or page.query_selector(".ant-btn-primary")
            if btn:
                btn.click()
                page.wait_for_timeout(_DEFAULT_WAIT_MS)
                logger.info("[FORM] Playwright: submitted via {!r}", submit_sel)
                if recorder:
                    recorder.step("web", action_type_from_config(config), substitutions,
                                  narration="Filled and submitted the form")
                    recorder.checkpoint()
                preview = substitutions.get("text", "")
                preview = preview[:60] + "..." if len(preview) > 60 else preview
                return f'Done. Submitted: "{preview}"'
            return "Form filled but submit button not found."

        return "Form filled."

    except Exception as exc:
        logger.exception("[FORM] Playwright error: {}", exc)
        return f"Form submission error: {exc}"
    finally:
        page.wait_for_timeout(1_500)
        close_page(pw, browser)


# ── Utilities ─────────────────────────────────────────────────────────────────

def _substitute(template: str, subs: dict) -> str:
    for key, val in subs.items():
        template = template.replace(f"{{{key}}}", val)
    return template


def action_type_from_config(config: dict) -> str:
    """Derive a short action type label from the config URL for recorder steps."""
    return config.get("url", "form_submit").lstrip("/").replace("/", "_")
