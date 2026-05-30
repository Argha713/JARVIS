"""
FormSubmitAction: fills and submits a form using config stored in SQLite.
Always uses a visible browser. Config format:
{
  "url":    "/my-apps/work-journal",
  "fields": [{"selector": "textarea", "value": "{text}"}],
  "submit": "button[type='submit']",
  "wait_ms": 2000
}
The {text} placeholder is replaced with the runtime value passed by the caller.
"""
import json
from loguru import logger

from tools.web_engine import store
from tools.web_engine.actions.navigate import open_page, close_page

_DEFAULT_WAIT_MS = 2_000


def run(site_id: str, action_type: str, substitutions: dict) -> str:
    """
    Loads the action config for site_id+action_type, opens a visible browser,
    fills fields (replacing {key} placeholders with substitutions), submits.
    Returns a result string.
    """
    config = store.get_action(site_id, action_type)
    if not config:
        return f"No action config found for {action_type!r} on {site_id}."

    site = store.get_site(site_id)
    base_url = site["base_url"] if site else f"https://{site_id}"
    url = base_url.rstrip("/") + "/" + config["url"].lstrip("/")

    result = open_page(site_id, url, headless=False)
    if not result:
        return "Could not open the browser for this action."

    pw, browser, context, page = result
    try:
        page.wait_for_timeout(config.get("wait_ms", _DEFAULT_WAIT_MS))

        for field in config.get("fields", []):
            selector = field["selector"]
            value    = _substitute(field["value"], substitutions)
            el = page.query_selector(selector)
            if not el:
                # Try contenteditable fallback
                el = page.query_selector("[contenteditable='true']")
            if el:
                el.click()
                page.keyboard.press("Control+a")
                if selector == "textarea" or "input" in selector:
                    el.fill(value)
                else:
                    page.keyboard.type(value)
                logger.info("[FORM] Filled {!r} with {} chars", selector, len(value))
            else:
                logger.warning("[FORM] Field not found: {!r}", selector)

        submit_sel = config.get("submit")
        if submit_sel:
            submit = page.query_selector(submit_sel)
            if not submit:
                submit = page.query_selector(".ant-btn-primary")
            if submit:
                submit.click()
                page.wait_for_timeout(_DEFAULT_WAIT_MS)
                logger.info("[FORM] Form submitted via {!r}", submit_sel)
                preview = substitutions.get("text", "")
                preview = preview[:60] + "..." if len(preview) > 60 else preview
                return f'Done. Submitted: "{preview}"'
            else:
                return "Form filled but submit button not found."

        return "Form filled."

    except Exception as e:
        logger.exception("[FORM] Error during form submit: {}", e)
        return f"Form submission error: {e}"
    finally:
        page.wait_for_timeout(1_500)
        close_page(pw, browser)


def _substitute(template: str, subs: dict) -> str:
    for key, val in subs.items():
        template = template.replace(f"{{{key}}}", val)
    return template
