"""
Storage layer for the Web Intelligence Engine.
SQLite for metadata/flows/cache; ChromaDB for section embeddings.
"""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import chromadb
from loguru import logger

_DB_PATH    = "data/web_engine.db"
_CHROMA_PATH = "data/chromadb"
_COLLECTION  = "portal_sections"

CACHE_TTL_HOURS = 24


# ─────────────────────────────────────────────
# SQLite helpers
# ─────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


@contextmanager
def _db():
    con = _conn()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    """Create all tables if they don't exist. Safe to call on every startup."""
    with _db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS sites (
            id                  TEXT PRIMARY KEY,
            base_url            TEXT NOT NULL,
            name                TEXT,
            session_data        TEXT,
            session_expires_at  TEXT,
            last_login_at       TEXT
        );

        CREATE TABLE IF NOT EXISTS site_tags (
            site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
            tag     TEXT NOT NULL,
            PRIMARY KEY (site_id, tag)
        );

        CREATE TABLE IF NOT EXISTS pages (
            id                  TEXT PRIMARY KEY,
            site_id             TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
            url                 TEXT NOT NULL,
            name                TEXT,
            last_discovered_at  TEXT,
            last_validated_at   TEXT,
            UNIQUE(site_id, url)
        );

        CREATE TABLE IF NOT EXISTS sections (
            id           TEXT PRIMARY KEY,
            page_id      TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
            label        TEXT,
            selector     TEXT,
            last_seen_at TEXT
        );

        CREATE TABLE IF NOT EXISTS cache (
            section_id   TEXT PRIMARY KEY REFERENCES sections(id) ON DELETE CASCADE,
            data         TEXT NOT NULL,
            extracted_at TEXT NOT NULL,
            expires_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS actions (
            id           TEXT PRIMARY KEY,
            site_id      TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
            type         TEXT NOT NULL,
            config       TEXT NOT NULL,
            last_used_at TEXT
        );

        CREATE TABLE IF NOT EXISTS pending_queries (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            query      TEXT NOT NULL,
            site_id    TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            status     TEXT DEFAULT 'pending'
        );

        CREATE TABLE IF NOT EXISTS section_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            section_id  TEXT NOT NULL,
            data        TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_section_history
            ON section_history(section_id, recorded_at);

        CREATE TABLE IF NOT EXISTS api_endpoints (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id         TEXT    NOT NULL,
            page_name       TEXT    NOT NULL,
            url             TEXT    NOT NULL,
            method          TEXT    NOT NULL DEFAULT 'GET',
            sample_response TEXT,
            request_body    TEXT,
            discovered_at   TEXT    NOT NULL,
            last_used_at    TEXT,
            UNIQUE(site_id, page_name, url)
        );
        """)
    # Migrate existing DB: add request_body column if the table was created before this field
    try:
        with _db() as con:
            con.execute("ALTER TABLE api_endpoints ADD COLUMN request_body TEXT")
        logger.debug("[STORE] Migrated api_endpoints: added request_body column")
    except Exception:
        pass  # Column already exists — normal on fresh start after schema update
    logger.debug("[STORE] DB initialised at {}", _DB_PATH)


# ─────────────────────────────────────────────
# Sites
# ─────────────────────────────────────────────

def upsert_site(site_id: str, base_url: str, name: str = "") -> None:
    with _db() as con:
        con.execute("""
            INSERT INTO sites (id, base_url, name)
            VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                base_url = excluded.base_url,
                name     = COALESCE(excluded.name, sites.name)
        """, (site_id, base_url, name))


def get_site(site_id: str) -> Optional[sqlite3.Row]:
    with _db() as con:
        return con.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()


def site_has_sections(site_id: str) -> bool:
    """Return True if this site has ever had sections indexed (i.e. been visited before)."""
    con = _conn()
    try:
        row = con.execute(
            "SELECT 1 FROM sections s JOIN pages p ON s.page_id = p.id WHERE p.site_id = ? LIMIT 1",
            (site_id,)
        ).fetchone()
        return row is not None
    finally:
        con.close()


def save_session(site_id: str, session_data: dict) -> None:
    expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    with _db() as con:
        con.execute("""
            UPDATE sites SET session_data=?, session_expires_at=?, last_login_at=?
            WHERE id=?
        """, (json.dumps(session_data), expires, _now(), site_id))
    logger.debug("[STORE] Session saved for {}", site_id)


def load_session(site_id: str) -> Optional[dict]:
    with _db() as con:
        row = con.execute(
            "SELECT session_data, session_expires_at FROM sites WHERE id=?",
            (site_id,)
        ).fetchone()
    if not row or not row["session_data"]:
        return None
    expires = row["session_expires_at"]
    if expires and datetime.fromisoformat(expires) < datetime.now(timezone.utc):
        logger.debug("[STORE] Session for {} expired", site_id)
        return None
    return json.loads(row["session_data"])


def all_sites() -> list[sqlite3.Row]:
    with _db() as con:
        return con.execute("SELECT * FROM sites").fetchall()


# ─────────────────────────────────────────────
# Site tags
# ─────────────────────────────────────────────

def add_tag(site_id: str, tag: str) -> None:
    tag = tag.strip().lower()
    if not tag:
        return
    with _db() as con:
        con.execute(
            "INSERT OR IGNORE INTO site_tags (site_id, tag) VALUES (?, ?)",
            (site_id, tag)
        )
    logger.debug("[STORE] Tag {!r} added for {}", tag, site_id)


def get_tags(site_id: str) -> list[str]:
    with _db() as con:
        rows = con.execute(
            "SELECT tag FROM site_tags WHERE site_id=?", (site_id,)
        ).fetchall()
    return [r["tag"] for r in rows]


def all_tags() -> list[tuple[str, str]]:
    """Returns [(site_id, tag), ...] for all sites."""
    with _db() as con:
        rows = con.execute("SELECT site_id, tag FROM site_tags").fetchall()
    return [(r["site_id"], r["tag"]) for r in rows]


# ─────────────────────────────────────────────
# Pages
# ─────────────────────────────────────────────

def upsert_page(site_id: str, url: str, name: str = "") -> str:
    """Insert or update a page; returns page id."""
    with _db() as con:
        row = con.execute(
            "SELECT id FROM pages WHERE site_id=? AND url=?", (site_id, url)
        ).fetchone()
        if row:
            page_id = row["id"]
            con.execute(
                "UPDATE pages SET last_discovered_at=?, name=COALESCE(?,name) WHERE id=?",
                (_now(), name or None, page_id)
            )
        else:
            page_id = str(uuid.uuid4())
            con.execute("""
                INSERT INTO pages (id, site_id, url, name, last_discovered_at)
                VALUES (?, ?, ?, ?, ?)
            """, (page_id, site_id, url, name, _now()))
    return page_id


def get_pages_for_site(site_id: str) -> list[sqlite3.Row]:
    with _db() as con:
        return con.execute(
            "SELECT * FROM pages WHERE site_id=? AND name IS NOT NULL AND name != ''",
            (site_id,)
        ).fetchall()


def get_page_by_url(site_id: str, url: str) -> Optional[sqlite3.Row]:
    with _db() as con:
        return con.execute(
            "SELECT * FROM pages WHERE site_id=? AND url=?", (site_id, url)
        ).fetchone()


def pages_needing_validation(max_age_days: int = 7) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    with _db() as con:
        return con.execute("""
            SELECT * FROM pages
            WHERE last_validated_at IS NULL OR last_validated_at < ?
        """, (cutoff,)).fetchall()


def mark_page_validated(page_id: str) -> None:
    with _db() as con:
        con.execute(
            "UPDATE pages SET last_validated_at=? WHERE id=?", (_now(), page_id)
        )


# ─────────────────────────────────────────────
# Sections
# ─────────────────────────────────────────────

def upsert_section(page_id: str, label: str, selector: str = "") -> str:
    """Insert or update a section by label; returns section id."""
    with _db() as con:
        row = con.execute(
            "SELECT id FROM sections WHERE page_id=? AND label=?", (page_id, label)
        ).fetchone()
        if row:
            section_id = row["id"]
            con.execute(
                "UPDATE sections SET selector=?, last_seen_at=? WHERE id=?",
                (selector, _now(), section_id)
            )
        else:
            section_id = str(uuid.uuid4())
            con.execute("""
                INSERT INTO sections (id, page_id, label, selector, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
            """, (section_id, page_id, label, selector, _now()))
    return section_id


def get_sections_for_page(page_id: str) -> list[sqlite3.Row]:
    with _db() as con:
        return con.execute(
            "SELECT * FROM sections WHERE page_id=?", (page_id,)
        ).fetchall()


def get_section(section_id: str) -> Optional[sqlite3.Row]:
    with _db() as con:
        return con.execute(
            "SELECT * FROM sections WHERE id=?", (section_id,)
        ).fetchone()


# ─────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────

def set_cache(section_id: str, data: str) -> None:
    now = _now()
    expires = (datetime.now(timezone.utc) + timedelta(hours=CACHE_TTL_HOURS)).isoformat()
    with _db() as con:
        con.execute("""
            INSERT INTO cache (section_id, data, extracted_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(section_id) DO UPDATE SET
                data=excluded.data,
                extracted_at=excluded.extracted_at,
                expires_at=excluded.expires_at
        """, (section_id, data, now, expires))
    logger.debug("[STORE] set_cache {} value={!r}", section_id[:8], data[:60])
    _record_history(section_id, data)


def get_cache(section_id: str) -> Optional[str]:
    with _db() as con:
        row = con.execute(
            "SELECT data, expires_at FROM cache WHERE section_id=?",
            (section_id,)
        ).fetchone()
    if not row:
        logger.debug("[STORE] get_cache {} → MISS (no row)", section_id[:8])
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        logger.debug("[STORE] get_cache {} → EXPIRED", section_id[:8])
        return None
    logger.debug("[STORE] get_cache {} → HIT value={!r}", section_id[:8], row["data"][:60])
    return row["data"]


def invalidate_cache(section_id: str) -> None:
    with _db() as con:
        con.execute("DELETE FROM cache WHERE section_id=?", (section_id,))


# ─────────────────────────────────────────────
# Actions
# ─────────────────────────────────────────────

def upsert_action(site_id: str, action_type: str, config: dict) -> str:
    with _db() as con:
        row = con.execute(
            "SELECT id FROM actions WHERE site_id=? AND type=?",
            (site_id, action_type)
        ).fetchone()
        if row:
            action_id = row["id"]
            con.execute(
                "UPDATE actions SET config=? WHERE id=?",
                (json.dumps(config), action_id)
            )
        else:
            action_id = str(uuid.uuid4())
            con.execute("""
                INSERT INTO actions (id, site_id, type, config)
                VALUES (?, ?, ?, ?)
            """, (action_id, site_id, action_type, json.dumps(config)))
    return action_id


def get_action(site_id: str, action_type: str) -> Optional[dict]:
    with _db() as con:
        row = con.execute(
            "SELECT config FROM actions WHERE site_id=? AND type=?",
            (site_id, action_type)
        ).fetchone()
    return json.loads(row["config"]) if row else None


def add_pending_query(query: str, site_id: str) -> int:
    """Save a query that timed out for later retry."""
    with _db() as con:
        cur = con.execute(
            "INSERT INTO pending_queries (query, site_id) VALUES (?, ?)",
            (query, site_id),
        )
        return cur.lastrowid


def get_all_actions_for_site(site_id: str) -> list[dict]:
    """Returns [{type, config}, ...] for all actions registered for a site."""
    with _db() as con:
        rows = con.execute(
            "SELECT type, config FROM actions WHERE site_id=?", (site_id,)
        ).fetchall()
    return [{"type": r["type"], "config": json.loads(r["config"])} for r in rows]


# ─────────────────────────────────────────────
# API endpoints
# ─────────────────────────────────────────────

def save_api_endpoint(site_id: str, page_name: str, url: str,
                      method: str, sample_response: str,
                      request_body: str = None) -> None:
    with _db() as con:
        con.execute(
            """INSERT INTO api_endpoints
               (site_id, page_name, url, method, sample_response, request_body, discovered_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(site_id, page_name, url) DO UPDATE SET
                   sample_response = excluded.sample_response,
                   method          = excluded.method,
                   request_body    = excluded.request_body,
                   discovered_at   = excluded.discovered_at""",
            (site_id, page_name, url, method, sample_response, request_body, _now())
        )
    logger.debug("[STORE] API endpoint saved: {} {} (page={}) body={}",
                 method, url, page_name, "yes" if request_body else "none")


def get_api_endpoints(site_id: str, page_name: str) -> list[dict]:
    with _db() as con:
        rows = con.execute(
            """SELECT id, url, method, sample_response, request_body FROM api_endpoints
               WHERE site_id=? AND page_name=?
               ORDER BY COALESCE(last_used_at, discovered_at) DESC""",
            (site_id, page_name)
        ).fetchall()
    return [{"id": r["id"], "url": r["url"], "method": r["method"],
             "sample": r["sample_response"], "body": r["request_body"]} for r in rows]


def touch_endpoint(endpoint_id: int) -> None:
    with _db() as con:
        con.execute(
            "UPDATE api_endpoints SET last_used_at=? WHERE id=?",
            (_now(), endpoint_id)
        )


# ─────────────────────────────────────────────
# ChromaDB — section embeddings
# ─────────────────────────────────────────────

def _chroma_collection():
    client = chromadb.PersistentClient(path=_CHROMA_PATH)
    return client.get_or_create_collection(_COLLECTION)


def index_section(section_id: str, label: str, value: str,
                  site_id: str, page_id: str, url: str) -> None:
    """Embed label+value and store in ChromaDB."""
    document = f"{label}: {value}" if value and value != label else label
    col = _chroma_collection()
    col.upsert(
        ids=[section_id],
        documents=[document],
        metadatas=[{
            "section_id": section_id,
            "page_id":    page_id,
            "site_id":    site_id,
            "url":        url,
            "label":      label,
        }]
    )
    logger.debug("[STORE] index_section {} doc={!r}", section_id[:8], document[:70])


def semantic_search(query: str, site_id: str = "", n: int = 5) -> list[dict]:
    """
    Returns up to n results: [{section_id, page_id, site_id, url, label, document, score}]
    Optionally filtered to a specific site.
    """
    col = _chroma_collection()
    total = col.count()
    logger.debug("[STORE] semantic_search query={!r} site={!r} n={} total_indexed={}",
                 query[:60], site_id, n, total)
    if total == 0:
        logger.debug("[STORE] semantic_search → ChromaDB empty")
        return []

    where = {"site_id": site_id} if site_id else None
    kwargs = {"query_texts": [query], "n_results": min(n, total)}
    if where:
        kwargs["where"] = where

    try:
        results = col.query(**kwargs)
    except Exception as e:
        logger.warning("[STORE] ChromaDB query failed: {}", e)
        return []

    hits = []
    ids       = results.get("ids",       [[]])[0]
    docs      = results.get("documents", [[]])[0]
    metas     = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]

    for i, sid in enumerate(ids):
        score = 1 - distances[i]
        hits.append({
            "section_id": sid,
            "page_id":    metas[i].get("page_id", ""),
            "site_id":    metas[i].get("site_id", ""),
            "url":        metas[i].get("url", ""),
            "label":      metas[i].get("label", ""),
            "document":   docs[i],
            "score":      score,
        })
        logger.debug("[STORE]   hit #{} score={:.3f} label={!r} doc={!r}",
                     i + 1, score, metas[i].get("label", "")[:40], docs[i][:50])
    return hits


def delete_section_embedding(section_id: str) -> None:
    col = _chroma_collection()
    try:
        col.delete(ids=[section_id])
    except Exception:
        pass


# ─────────────────────────────────────────────
# Section history
# ─────────────────────────────────────────────

def _record_history(section_id: str, data: str) -> None:
    """Record a new history entry only when the value actually changed. Keeps last 5."""
    with _db() as con:
        last = con.execute(
            "SELECT data FROM section_history WHERE section_id=? ORDER BY recorded_at DESC LIMIT 1",
            (section_id,)
        ).fetchone()
        if last and last["data"] == data:
            logger.debug("[STORE] history {} → UNCHANGED (skip)", section_id[:8])
            return  # value unchanged — skip
        prev = last["data"] if last else None
        con.execute(
            "INSERT INTO section_history (section_id, data, recorded_at) VALUES (?, ?, ?)",
            (section_id, data, _now())
        )
        # Prune: keep only the 5 most recent rows
        con.execute("""
            DELETE FROM section_history
            WHERE section_id=? AND id NOT IN (
                SELECT id FROM section_history
                WHERE section_id=? ORDER BY recorded_at DESC LIMIT 5
            )
        """, (section_id, section_id))
    if prev is None:
        logger.debug("[STORE] history {} → FIRST ENTRY value={!r}", section_id[:8], data[:40])
    else:
        logger.debug("[STORE] history {} → CHANGED {!r} → {!r}",
                     section_id[:8], prev[:40], data[:40])


def get_history(section_id: str, n: int = 5) -> list[dict]:
    """Returns up to n most-recent history rows: [{data, recorded_at}, ...]"""
    with _db() as con:
        rows = con.execute(
            "SELECT data, recorded_at FROM section_history WHERE section_id=? ORDER BY recorded_at DESC LIMIT ?",
            (section_id, n)
        ).fetchall()
    return [{"data": r["data"], "recorded_at": r["recorded_at"]} for r in rows]


def _human_time(iso: str) -> str:
    """Convert ISO timestamp to human-readable string (Windows-safe, no %-d)."""
    dt = datetime.fromisoformat(iso).astimezone()
    now = datetime.now(dt.tzinfo)
    delta = now - dt
    if delta.days == 0:
        return "earlier today"
    if delta.days == 1:
        return "yesterday"
    if delta.days < 7:
        return f"{delta.days} days ago"
    return dt.strftime("%d %b %Y").lstrip("0")


def format_with_history(section_id: str, current_value: str) -> str:
    """
    Wrap current_value with a history note if available.
    Returns the augmented answer string.
    """
    history = get_history(section_id, n=5)
    # history[0] is the most recent entry — just written by set_cache via _record_history
    # If there are 2+ entries, compare current to the previous one
    if len(history) < 2:
        return current_value

    prev = history[1]  # second-most-recent
    prev_value  = prev["data"]
    prev_time   = _human_time(prev["recorded_at"])

    if prev_value == current_value:
        return f"{current_value}\n\n(Same as {prev_time}.)"
    return f"{current_value}\n\n(Last time I checked {prev_time}, it was: {prev_value}.)"


# ─────────────────────────────────────────────
# Internal
# ─────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
