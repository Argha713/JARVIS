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
        """)
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


def get_cache(section_id: str) -> Optional[str]:
    with _db() as con:
        row = con.execute(
            "SELECT data, expires_at FROM cache WHERE section_id=?",
            (section_id,)
        ).fetchone()
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        logger.debug("[STORE] Cache expired for section {}", section_id)
        return None
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


def semantic_search(query: str, site_id: str = "", n: int = 5) -> list[dict]:
    """
    Returns up to n results: [{section_id, page_id, site_id, url, label, document, score}]
    Optionally filtered to a specific site.
    """
    col = _chroma_collection()
    if col.count() == 0:
        return []

    where = {"site_id": site_id} if site_id else None
    kwargs = {"query_texts": [query], "n_results": min(n, col.count())}
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
        hits.append({
            "section_id": sid,
            "page_id":    metas[i].get("page_id", ""),
            "site_id":    metas[i].get("site_id", ""),
            "url":        metas[i].get("url", ""),
            "label":      metas[i].get("label", ""),
            "document":   docs[i],
            "score":      1 - distances[i],   # cosine similarity (higher = better)
        })
    return hits


def delete_section_embedding(section_id: str) -> None:
    col = _chroma_collection()
    try:
        col.delete(ids=[section_id])
    except Exception:
        pass


# ─────────────────────────────────────────────
# Internal
# ─────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
