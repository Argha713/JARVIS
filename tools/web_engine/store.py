"""
Storage layer for the Web Intelligence Engine.
SQLite for metadata/flows/cache; ChromaDB for section embeddings.

Embedding model selection (reads config.json at startup):
  provider=openai  → OpenAI text-embedding-3-small (high quality, ~$0.0003/day)
  provider=ollama  → local all-MiniLM-L6-v2 (free, weaker on domain queries)
"""
import json
import sqlite3
import threading
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

# ─────────────────────────────────────────────
# ChromaDB embedding function (resolved once at startup)
# ─────────────────────────────────────────────

_embedding_fn = None   # None = not yet resolved
_embedding_fn_ready = False
_embedding_fn_lock = threading.Lock()


def _get_embedding_fn():
    """
    Return the ChromaDB embedding function to use, based on config.json.
      openai  → OpenAI text-embedding-3-small
      ollama  → default local all-MiniLM-L6-v2 (returns None = ChromaDB default)
    Result is cached after first call.
    """
    global _embedding_fn, _embedding_fn_ready
    # Double-checked lock: fast path for the common (already-initialised) case.
    if _embedding_fn_ready:
        return _embedding_fn
    with _embedding_fn_lock:
        if _embedding_fn_ready:
            return _embedding_fn

        try:
            cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
            provider = cfg.get("llm", {}).get("provider", "ollama")
            logger.info("[STORE] LLM provider={!r} → selecting ChromaDB embedding model", provider)

            if provider == "openai":
                api_key = cfg.get("llm", {}).get("openai_api_key", "")
                if api_key:
                    from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
                    _embedding_fn = OpenAIEmbeddingFunction(
                        api_key=api_key,
                        model_name="text-embedding-3-small",
                    )
                    logger.info("[STORE] ChromaDB embedding: OpenAI text-embedding-3-small "
                                "(1536 dims, ~$0.02/M tokens)")
                else:
                    logger.warning("[STORE] provider=openai but no API key found — "
                                   "falling back to local MiniLM")
                    _embedding_fn = None
            else:
                logger.info("[STORE] ChromaDB embedding: local all-MiniLM-L6-v2 (384 dims, free)")
                _embedding_fn = None   # None = ChromaDB default (MiniLM)

        except Exception as e:
            logger.warning("[STORE] Could not read config.json for embedding selection ({})"
                           " — using MiniLM default", e)
            _embedding_fn = None

        _embedding_fn_ready = True
        return _embedding_fn

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

        CREATE TABLE IF NOT EXISTS procedures (
            id                   TEXT PRIMARY KEY,
            name                 TEXT NOT NULL,
            tool                 TEXT NOT NULL,
            params_template      TEXT NOT NULL DEFAULT '{}',
            created_at           TEXT NOT NULL,
            last_used            TEXT,
            success_count        INTEGER DEFAULT 0,
            failure_count        INTEGER DEFAULT 0,
            consecutive_failures INTEGER DEFAULT 0,
            requires_variables   TEXT    DEFAULT '[]',
            narration_start      TEXT    DEFAULT '',
            narration_done       TEXT    DEFAULT '',
            narration_error      TEXT    DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS procedure_steps (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            procedure_id TEXT    NOT NULL REFERENCES procedures(id) ON DELETE CASCADE,
            step_order   INTEGER NOT NULL,
            tool         TEXT    NOT NULL,
            action       TEXT    NOT NULL,
            params       TEXT    NOT NULL DEFAULT '{}',
            narration    TEXT    DEFAULT '',
            sensitive    INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_procedure_steps_order
            ON procedure_steps(procedure_id, step_order);

        CREATE TABLE IF NOT EXISTS procedure_triggers (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            procedure_id TEXT NOT NULL REFERENCES procedures(id) ON DELETE CASCADE,
            phrase       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS personality_phrases (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            category   TEXT    NOT NULL,
            phrase     TEXT    NOT NULL,
            is_seed    INTEGER NOT NULL DEFAULT 0,
            created_at TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(category, phrase)
        );
        CREATE INDEX IF NOT EXISTS idx_personality_cat ON personality_phrases(category);

        CREATE TABLE IF NOT EXISTS personality_refresh (
            category       TEXT PRIMARY KEY,
            last_refreshed TEXT
        );

        CREATE TABLE IF NOT EXISTS whisper_hallucinations (
            phrase   TEXT PRIMARY KEY,
            is_seed  INTEGER NOT NULL DEFAULT 0,
            added_at TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS site_profiles (
            site_id   TEXT PRIMARY KEY,
            browser   TEXT NOT NULL,
            profile   TEXT NOT NULL,
            last_used TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS installed_profiles (
            browser         TEXT NOT NULL,
            profile         TEXT NOT NULL,
            installed_at    TEXT DEFAULT (datetime('now')),
            last_connected  TEXT,
            PRIMARY KEY (browser, profile)
        );

        CREATE TABLE IF NOT EXISTS browser_prefs (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- ── JARVIS system state ─────────────────────────────────────────────

        CREATE TABLE IF NOT EXISTS system_info (
            key        TEXT PRIMARY KEY,
            value      TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS browser_state (
            browser              TEXT NOT NULL,
            profile              TEXT NOT NULL,
            display_name         TEXT,
            exe_path             TEXT,
            is_installed         INTEGER DEFAULT 1,
            extension_installed  INTEGER DEFAULT 0,
            is_active            INTEGER DEFAULT 0,
            detected_at          TEXT,
            PRIMARY KEY (browser, profile)
        );

        -- Single-row table (id is always 1) for live extension connection state.
        CREATE TABLE IF NOT EXISTS extension_state (
            id              INTEGER PRIMARY KEY CHECK (id = 1),
            is_connected    INTEGER DEFAULT 0,
            active_browser  TEXT,
            active_profile  TEXT,
            connected_at    TEXT,
            disconnected_at TEXT
        );
        INSERT OR IGNORE INTO extension_state (id, is_connected) VALUES (1, 0);

        CREATE TABLE IF NOT EXISTS site_health (
            site_id             TEXT PRIMARY KEY REFERENCES sites(id) ON DELETE CASCADE,
            session_status      TEXT DEFAULT 'unknown',
            last_refresh_at     TEXT,
            last_section_count  INTEGER DEFAULT 0,
            last_checked_at     TEXT
        );
        """)

    # ── Column migrations (idempotent — ignored if column already exists) ──────
    _migrate("ALTER TABLE api_endpoints ADD COLUMN request_body TEXT",
             "api_endpoints: added request_body")
    _migrate("ALTER TABLE pages ADD COLUMN settle_ms INTEGER DEFAULT NULL",
             "pages: added settle_ms")
    _migrate("ALTER TABLE sites ADD COLUMN refresh_interval_minutes INTEGER DEFAULT 60",
             "sites: added refresh_interval_minutes")

    logger.debug("[STORE] DB initialised at {}", _DB_PATH)


def _migrate(sql: str, label: str) -> None:
    try:
        with _db() as con:
            con.execute(sql)
        logger.debug("[STORE] Migration applied — {}", label)
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            logger.error("[STORE] Migration error for {!r}: {}", label, e)
            raise


# ─────────────────────────────────────────────
# Sites
# ─────────────────────────────────────────────

def upsert_site(site_id: str, base_url: str, name: Optional[str] = None) -> None:
    with _db() as con:
        con.execute("""
            INSERT INTO sites (id, base_url, name)
            VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                base_url = excluded.base_url,
                name     = COALESCE(NULLIF(excluded.name, ''), sites.name)
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


def delete_sections(section_ids: list[str]) -> None:
    """
    Delete sections (and their cache entries via FK cascade) from SQLite.
    section_history has no FK so it is left as-is (orphaned rows are harmless).
    Called after extract_page() to purge labels no longer present on the page.
    """
    if not section_ids:
        return
    placeholders = ",".join("?" * len(section_ids))
    with _db() as con:
        con.execute(f"DELETE FROM sections WHERE id IN ({placeholders})", section_ids)
    logger.debug("[STORE] Deleted {} stale sections from SQLite", len(section_ids))


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
            """SELECT id, url, method, sample_response, request_body, discovered_at
               FROM api_endpoints
               WHERE site_id=? AND page_name=?
               ORDER BY COALESCE(last_used_at, discovered_at) DESC""",
            (site_id, page_name)
        ).fetchall()
    return [{"id": r["id"], "url": r["url"], "method": r["method"],
             "sample": r["sample_response"], "body": r["request_body"],
             "discovered_at": r["discovered_at"]} for r in rows]


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
    """
    Return the ChromaDB collection using the configured embedding function.

    Distance metric is always 'cosine' so that:
      score = 1 - cosine_distance  (ranges -1 to 1, genuine semantic similarity)

    Without this, ChromaDB defaults to 'l2' (Euclidean). OpenAI embeddings are
    not unit-normalized so l2 distances can exceed 1, making scores negative and
    meaningless. Even for MiniLM (unit vectors) cosine is more semantically correct.
    """
    client = chromadb.PersistentClient(path=_CHROMA_PATH)
    ef = _get_embedding_fn()
    kwargs = {"metadata": {"hnsw:space": "cosine"}}
    if ef is not None:
        kwargs["embedding_function"] = ef
    return client.get_or_create_collection(_COLLECTION, **kwargs)


def index_section(section_id: str, label: str, value: str,
                  site_id: str, page_id: str, url: str) -> None:
    """
    Embed the section label and store in ChromaDB.

    Only the LABEL is embedded (not "label: value") so that semantic search
    matches the concept cleanly. The value lives in the SQLite cache — ChromaDB
    is only responsible for finding which section best matches a query.

    Before: document = "Attendance Rate: 100 %"  → score ~0.21 for attendance query
    After:  document = "Attendance Rate"          → score ~0.65 for attendance query
    """
    document = label   # label only — values contaminate the embedding
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
    logger.debug("[STORE] index_section {} doc={!r} (label-only embedding)",
                 section_id[:8], document[:70])


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
    if delta.total_seconds() < 0:
        return "just now"
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

# Procedure CRUD → tools/web_engine/procedure_store.py
# Schema (tables) is created here in init_db() — single init point for the whole DB.


# ─────────────────────────────────────────────
# Personality phrases
# ─────────────────────────────────────────────

def seed_personality_phrase(category: str, phrase: str) -> None:
    with _db() as con:
        con.execute(
            "INSERT OR IGNORE INTO personality_phrases (category, phrase, is_seed) VALUES (?,?,1)",
            (category, phrase),
        )
        con.execute(
            "INSERT OR IGNORE INTO personality_refresh (category) VALUES (?)", (category,)
        )


def get_personality_phrases(category: str) -> list:
    with _db() as con:
        rows = con.execute(
            "SELECT phrase FROM personality_phrases WHERE category=?", (category,)
        ).fetchall()
    return [r["phrase"] for r in rows]


def get_personality_last_refreshed(category: str):
    with _db() as con:
        row = con.execute(
            "SELECT last_refreshed FROM personality_refresh WHERE category=?", (category,)
        ).fetchone()
    return row["last_refreshed"] if row else None


def replace_llm_personality_phrases(category: str, phrases: list) -> None:
    """Atomic swap: delete old LLM phrases, insert new ones. Seeds are untouched."""
    with _db() as con:
        con.execute(
            "DELETE FROM personality_phrases WHERE category=? AND is_seed=0", (category,)
        )
        con.executemany(
            "INSERT OR IGNORE INTO personality_phrases (category, phrase, is_seed) VALUES (?,?,0)",
            [(category, p) for p in phrases],
        )


def mark_personality_refreshed(category: str) -> None:
    with _db() as con:
        con.execute(
            "UPDATE personality_refresh SET last_refreshed=datetime('now') WHERE category=?",
            (category,),
        )


# ─────────────────────────────────────────────
# Whisper hallucinations
# ─────────────────────────────────────────────

def seed_hallucination(phrase: str) -> None:
    with _db() as con:
        con.execute(
            "INSERT OR IGNORE INTO whisper_hallucinations (phrase, is_seed) VALUES (?,1)", (phrase,)
        )


def get_hallucinations() -> set:
    with _db() as con:
        rows = con.execute("SELECT phrase FROM whisper_hallucinations").fetchall()
    return {r["phrase"] for r in rows}


# ─────────────────────────────────────────────
# Browser integration
# ─────────────────────────────────────────────

def get_site_profile(site_id: str) -> dict | None:
    """Return the saved browser+profile for a site, or None if not known."""
    with _db() as con:
        row = con.execute(
            "SELECT browser, profile FROM site_profiles WHERE site_id=?", (site_id,)
        ).fetchone()
    if row:
        logger.debug("[STORE] get_site_profile {!r} → browser={!r} profile={!r}", site_id, row["browser"], row["profile"])
        return {"browser": row["browser"], "profile": row["profile"]}
    logger.debug("[STORE] get_site_profile {!r} → None (not saved)", site_id)
    return None


def save_site_profile(site_id: str, browser: str, profile: str) -> None:
    """Upsert the browser+profile used for a site."""
    logger.info("[STORE] save_site_profile site={!r} browser={!r} profile={!r}", site_id, browser, profile)
    with _db() as con:
        con.execute(
            "INSERT OR REPLACE INTO site_profiles(site_id, browser, profile, last_used) "
            "VALUES(?, ?, ?, datetime('now'))",
            (site_id, browser, profile),
        )


def is_profile_installed(browser: str, profile: str) -> bool:
    """Return True if this browser+profile has previously had the extension installed."""
    with _db() as con:
        row = con.execute(
            "SELECT 1 FROM installed_profiles WHERE browser=? AND profile=?", (browser, profile)
        ).fetchone()
    result = row is not None
    logger.debug("[STORE] is_profile_installed browser={!r} profile={!r} → {}", browser, profile, result)
    return result


def mark_profile_installed(browser: str, profile: str) -> None:
    """Record that the extension was installed in this browser+profile."""
    logger.info("[STORE] mark_profile_installed browser={!r} profile={!r}", browser, profile)
    with _db() as con:
        con.execute(
            "INSERT OR IGNORE INTO installed_profiles(browser, profile) VALUES(?, ?)",
            (browser, profile),
        )


def mark_profile_connected(browser: str, profile: str) -> None:
    """Update last_connected timestamp for this browser+profile."""
    logger.debug("[STORE] mark_profile_connected browser={!r} profile={!r}", browser, profile)
    with _db() as con:
        con.execute(
            "UPDATE installed_profiles SET last_connected=datetime('now') WHERE browser=? AND profile=?",
            (browser, profile),
        )


def get_last_connected_profile(browser: str) -> str | None:
    """Return the most recently connected profile for a browser, or None."""
    with _db() as con:
        row = con.execute(
            "SELECT profile FROM installed_profiles WHERE browser=? "
            "ORDER BY last_connected DESC LIMIT 1",
            (browser,),
        ).fetchone()
    profile = row["profile"] if row else None
    logger.debug("[STORE] get_last_connected_profile browser={!r} → {!r}", browser, profile)
    return profile


def get_browser_pref(key: str) -> str | None:
    """Read a generic browser preference value."""
    with _db() as con:
        row = con.execute("SELECT value FROM browser_prefs WHERE key=?", (key,)).fetchone()
    value = row["value"] if row else None
    logger.debug("[STORE] get_browser_pref {!r} → {!r}", key, value)
    return value


def set_browser_pref(key: str, value: str) -> None:
    """Write a generic browser preference value."""
    logger.info("[STORE] set_browser_pref {!r} = {!r}", key, value)
    with _db() as con:
        con.execute(
            "INSERT OR REPLACE INTO browser_prefs(key, value) VALUES(?, ?)", (key, value)
        )


# ─────────────────────────────────────────────
# System info
# ─────────────────────────────────────────────

def system_info_set(key: str, value: str) -> None:
    with _db() as con:
        con.execute(
            "INSERT OR REPLACE INTO system_info(key, value, updated_at) VALUES(?,?,?)",
            (key, value, _now()),
        )


def system_info_get(key: str) -> str | None:
    with _db() as con:
        row = con.execute("SELECT value FROM system_info WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def system_info_set_all(platform: str, version: str, hostname: str) -> None:
    """Write OS/machine info in one call."""
    now = _now()
    with _db() as con:
        for k, v in (("os_platform", platform), ("os_version", version), ("hostname", hostname)):
            con.execute(
                "INSERT OR REPLACE INTO system_info(key, value, updated_at) VALUES(?,?,?)",
                (k, v, now),
            )
    logger.info("[STORE] system_info saved: platform={!r} version={!r} hostname={!r}",
                platform, version, hostname)


# ─────────────────────────────────────────────
# Browser state
# ─────────────────────────────────────────────

def browser_state_upsert(
    browser: str,
    profile: str,
    display_name: str = "",
    exe_path: str = "",
    is_installed: bool = True,
    extension_installed: bool = False,
    is_active: bool = False,
) -> None:
    with _db() as con:
        con.execute("""
            INSERT INTO browser_state
                (browser, profile, display_name, exe_path,
                 is_installed, extension_installed, is_active, detected_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(browser, profile) DO UPDATE SET
                display_name        = excluded.display_name,
                exe_path            = excluded.exe_path,
                is_installed        = excluded.is_installed,
                is_active           = excluded.is_active,
                detected_at         = excluded.detected_at
        """, (browser, profile, display_name, exe_path,
              int(is_installed), int(extension_installed), int(is_active), _now()))


def browser_state_set_extension_installed(browser: str, profile: str, installed: bool) -> None:
    """Mark whether the JARVIS extension is installed in this browser+profile."""
    with _db() as con:
        con.execute(
            "INSERT INTO browser_state(browser, profile, extension_installed, detected_at) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(browser, profile) DO UPDATE SET extension_installed=excluded.extension_installed",
            (browser, profile, int(installed), _now()),
        )
    logger.info("[STORE] browser_state extension_installed={} for {}:{}", installed, browser, profile)


def browser_state_set_active(browser: str, profile: str) -> None:
    """Mark a browser+profile as the active one (clears is_active on all others)."""
    with _db() as con:
        con.execute("UPDATE browser_state SET is_active=0")
        con.execute(
            "INSERT INTO browser_state(browser, profile, is_active, detected_at) "
            "VALUES(?,?,1,?) "
            "ON CONFLICT(browser, profile) DO UPDATE SET is_active=1",
            (browser, profile, _now()),
        )


def browser_state_get(browser: str, profile: str) -> dict | None:
    with _db() as con:
        row = con.execute(
            "SELECT * FROM browser_state WHERE browser=? AND profile=?", (browser, profile)
        ).fetchone()
    return dict(row) if row else None


def browser_state_get_active() -> dict | None:
    """Return the currently active browser+profile row, or None."""
    with _db() as con:
        row = con.execute(
            "SELECT * FROM browser_state WHERE is_active=1 LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def extension_installed_any() -> bool:
    """Return True if ANY browser+profile has the JARVIS extension installed."""
    with _db() as con:
        row = con.execute(
            "SELECT 1 FROM browser_state WHERE extension_installed=1 LIMIT 1"
        ).fetchone()
    return row is not None


# ─────────────────────────────────────────────
# Extension connection state
# ─────────────────────────────────────────────

def extension_state_set_connected(browser: str, profile: str) -> None:
    """Record that the extension just connected."""
    with _db() as con:
        con.execute("""
            UPDATE extension_state
            SET is_connected=1, active_browser=?, active_profile=?,
                connected_at=?, disconnected_at=NULL
            WHERE id=1
        """, (browser, profile, _now()))
    logger.info("[STORE] extension_state → connected ({}:{})", browser, profile)


def extension_state_set_disconnected() -> None:
    """Record that the extension disconnected."""
    with _db() as con:
        con.execute("""
            UPDATE extension_state
            SET is_connected=0, disconnected_at=?
            WHERE id=1
        """, (_now(),))
    logger.info("[STORE] extension_state → disconnected")


def extension_state_get() -> dict | None:
    with _db() as con:
        row = con.execute("SELECT * FROM extension_state WHERE id=1").fetchone()
    return dict(row) if row else None


# ─────────────────────────────────────────────
# Site health
# ─────────────────────────────────────────────

def site_health_upsert(
    site_id: str,
    session_status: str,
    last_section_count: int = 0,
) -> None:
    now = _now()
    with _db() as con:
        con.execute("""
            INSERT INTO site_health(site_id, session_status, last_refresh_at,
                                    last_section_count, last_checked_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(site_id) DO UPDATE SET
                session_status     = excluded.session_status,
                last_refresh_at    = CASE WHEN excluded.last_section_count > 0
                                          THEN excluded.last_refresh_at
                                          ELSE site_health.last_refresh_at END,
                last_section_count = excluded.last_section_count,
                last_checked_at    = excluded.last_checked_at
        """, (site_id, session_status, now, last_section_count, now))
    logger.debug("[STORE] site_health {} → status={!r} sections={}", site_id, session_status, last_section_count)


def site_health_get(site_id: str) -> dict | None:
    with _db() as con:
        row = con.execute("SELECT * FROM site_health WHERE site_id=?", (site_id,)).fetchone()
    return dict(row) if row else None


def site_health_get_all() -> dict[str, dict]:
    """Returns {site_id: health_dict} for all sites that have health records."""
    with _db() as con:
        rows = con.execute("SELECT * FROM site_health").fetchall()
    return {r["site_id"]: dict(r) for r in rows}


# ─────────────────────────────────────────────
# Page scheduling helpers
# ─────────────────────────────────────────────

def save_page_settle_ms(page_id: str, ms: int) -> None:
    """Persist the learned React render settle time for a page."""
    with _db() as con:
        con.execute("UPDATE pages SET settle_ms=? WHERE id=?", (ms, page_id))
    logger.debug("[STORE] page {} settle_ms={}", page_id[:8], ms)


def get_pages_for_refresh() -> list[dict]:
    """
    Return all pages joined with their site's refresh_interval_minutes and settle_ms.
    Used by bg_refresher to build the per-page schedule.
    """
    with _db() as con:
        rows = con.execute("""
            SELECT p.id, p.site_id, p.url, p.name,
                   p.settle_ms, p.last_validated_at,
                   COALESCE(s.refresh_interval_minutes, 60) AS refresh_interval_minutes
            FROM pages p
            JOIN sites s ON p.site_id = s.id
            ORDER BY p.site_id, p.url
        """).fetchall()
    return [dict(r) for r in rows]


def invalidate_site_cache(site_id: str) -> None:
    """
    Delete all SQLite cache values for every section on every page of site_id.
    ChromaDB embeddings (labels) are left intact so JARVIS still knows what sections exist.
    Called by bg_refresher when session expiry is detected (0 sections returned).
    """
    with _db() as con:
        con.execute("""
            DELETE FROM cache
            WHERE section_id IN (
                SELECT s.id FROM sections s
                JOIN pages p ON s.page_id = p.id
                WHERE p.site_id = ?
            )
        """, (site_id,))
    logger.warning("[STORE] invalidate_site_cache {} — all cached values deleted", site_id)
