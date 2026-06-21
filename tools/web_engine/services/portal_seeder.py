import json
from pathlib import Path

from loguru import logger

from tools.web_engine import store

PORTAL_SITE_ID = "people.codeclouds.com"
_PORTAL_BASE   = "https://people.codeclouds.com"
_PORTAL_TAGS   = [
    # Site identity
    "office portal", "hr portal", "portal", "simplified hr",
    "paipa", "codeclouds", "people", "work portal",
    # HR data keywords — pre-route without LLM
    "attendance", "punctuality", "leave", "leaves", "casual leave", "sick leave",
    "activity", "activity rate", "activity percentage", "eod", "end of day",
    "work journal", "seat booking", "seat", "support ticket", "request",
    "salary", "payslip", "holiday", "timesheet", "check in", "check out",
]


def seed(config: dict) -> None:
    """Register the office portal, sync all tags, upsert seeded actions, migrate legacy session."""
    if not store.get_site(PORTAL_SITE_ID):
        store.upsert_site(PORTAL_SITE_ID, _PORTAL_BASE, "Simplified HR Portal")

    for tag in _PORTAL_TAGS:
        store.add_tag(PORTAL_SITE_ID, tag)

    store.upsert_action(PORTAL_SITE_ID, "form_submit_eod", {
        "url":    "/my-apps/work-journal",
        "fields": [{"selector": "textarea", "value": "{text}"}],
        "submit": "button[type='submit']",
        "wait_ms": 3000,
    })
    store.upsert_action(PORTAL_SITE_ID, "page_activity", {
        "url":      "/my-activity",
        "keywords": ["attendance", "activity", "punctuality", "check in", "check out",
                     "active hours", "activity rate", "activity percentage"],
    })
    store.upsert_action(PORTAL_SITE_ID, "page_leave", {
        "url":      "/leave",
        "keywords": ["leave", "leaves", "casual leave", "sick leave", "leave balance",
                     "leave remaining", "leave count"],
    })
    store.upsert_action(PORTAL_SITE_ID, "page_requests", {
        "url":      "/requests",
        "keywords": ["request", "ticket", "support ticket", "support request"],
    })
    store.upsert_action(PORTAL_SITE_ID, "page_work_journal", {
        "url":      "/my-apps/work-journal",
        "keywords": ["work journal", "journal", "today task", "today work"],
    })

    _migrate_legacy_session()
    logger.info("[PortalSeeder] Seeded with {} tags", len(_PORTAL_TAGS))


def query_to_page_name(site_id: str, query: str) -> str | None:
    """Map a query to the portal page_name used by api_client endpoint lookup."""
    if site_id != PORTAL_SITE_ID:
        return None
    q = query.lower()
    if any(w in q for w in ("attendance", "punctuality", "activity", "check in",
                             "check out", "active hours", "working hours",
                             "punctual", "hours worked")):
        return "activity"
    if any(w in q for w in ("leave", "casual leave", "sick leave", "leave balance",
                             "leaves remaining", "leaves left")):
        return "leave"
    if any(w in q for w in ("request", "ticket", "support ticket", "support request")):
        return "requests"
    return None


def _migrate_legacy_session() -> None:
    legacy = Path("data/portal_session.json")
    if not legacy.exists():
        return
    try:
        session_data = json.loads(legacy.read_text(encoding="utf-8"))
        store.save_session(PORTAL_SITE_ID, session_data)
        logger.info("[PortalSeeder] Migrated legacy portal session to SQLite")
    except Exception as exc:
        logger.warning("[PortalSeeder] Could not migrate legacy session: {}", exc)
