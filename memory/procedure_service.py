"""
ProcedureService — service layer for procedure memory (Phase 4).

Coordinates two storage backends:
  SQLite  (via tools/web_engine/procedure_repo.py) — structured data, steps, metadata
  ChromaDB (procedure_triggers collection)          — semantic trigger embeddings

Rule: SQLite is written first. If ChromaDB then fails, the SQLite insert is
rolled back. This keeps both stores in sync — a procedure either exists in both
or neither. A procedure in SQLite but not ChromaDB would save fine but never match.

In .NET terms: this is the service layer. The SQL functions in
tools/web_engine/procedure_repo.py are the repository (DAL).
"""
import json
from pathlib import Path
from typing import Optional

import chromadb
from loguru import logger

from tools.web_engine import procedure_repo as sql
from tools.web_engine.store import _get_embedding_fn

_CHROMA_PATH       = "data/chromadb"
_PROC_COLLECTION   = "procedure_triggers"
_DEFAULT_THRESHOLD = 3   # consecutive failures before re-learn


def _read_broken_threshold() -> int:
    try:
        cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
        return cfg.get("procedures", {}).get("broken_procedure_threshold", _DEFAULT_THRESHOLD)
    except Exception:
        return _DEFAULT_THRESHOLD


def _chroma_proc_collection():
    """
    Return the procedure_triggers ChromaDB collection.
    Uses the same embedding function as portal_sections — cosine distance space.
    """
    client = chromadb.PersistentClient(path=_CHROMA_PATH)
    ef = _get_embedding_fn()
    kwargs = {"metadata": {"hnsw:space": "cosine"}}
    if ef is not None:
        kwargs["embedding_function"] = ef
    return client.get_or_create_collection(_PROC_COLLECTION, **kwargs)


def _chroma_doc_id(procedure_id: str, index: int) -> str:
    """Stable, unique ID for a trigger document in ChromaDB."""
    return f"{procedure_id}__{index}"


class ProcedureService:
    """
    High-level procedure service. Instantiated once by TaskRouter.
    All public methods are synchronous — call from a thread executor if needed.
    """

    def __init__(self):
        self._broken_threshold = _read_broken_threshold()
        # Touch the collection at startup so it's ready (creates it if new).
        try:
            _chroma_proc_collection()
            logger.info("[PROC] ChromaDB collection '{}' ready", _PROC_COLLECTION)
        except Exception as e:
            logger.warning("[PROC] Could not pre-warm ChromaDB collection: {}", e)

    # ──────────────────────────────────────────
    # Save (first time)
    # ──────────────────────────────────────────

    def save(self, name: str, tool: str, params_template: dict,
             triggers: list[str], steps: list[dict],
             requires_variables: list[str], narration: dict) -> str:
        """
        Save a new procedure. Returns procedure_id.

        Atomic: SQLite is written first. If ChromaDB embedding then fails,
        the SQLite rows are deleted (rolled back) and the exception re-raised.

        steps:  list of dicts from ProcedureRecorder.commit() — stored as metadata
                for monitoring/debugging. NOT individually executed at replay time.
        narration: {"start": "...", "done": "...", "error": "..."}
        """
        # 1. Write to SQLite
        proc_id = sql.save_procedure(name, tool, params_template,
                                     requires_variables, narration)
        for step in steps:
            sql.save_procedure_step(
                proc_id,
                step["step_order"],
                step["tool"],
                step["action"],
                step["params"],
                step.get("narration", ""),
                step.get("sensitive", False),
            )
        for phrase in triggers:
            sql.save_procedure_trigger(proc_id, phrase)

        # 2. Embed triggers → ChromaDB
        try:
            self._embed_triggers(proc_id, name, triggers)
        except Exception as e:
            logger.error("[PROC] ChromaDB embed failed for '{}' — rolling back SQLite: {}", name, e)
            sql.delete_procedure(proc_id)   # cascade removes steps + triggers
            raise

        logger.info("[PROC] saved '{}' id={} triggers={}", name, proc_id, len(triggers))
        return proc_id

    # ──────────────────────────────────────────
    # Overwrite (correction / re-learn)
    # ──────────────────────────────────────────

    def overwrite(self, procedure_id: str, tool: str, params_template: dict,
                  triggers: list[str], steps: list[dict],
                  requires_variables: list[str]) -> None:
        """
        Replace steps, triggers, tool, and params_template for an existing procedure.
        Resets consecutive_failures to 0.

        Used when the user corrects JARVIS ("next time use accuweather")
        or when a procedure breaks and is re-learned.
        """
        # 1. Delete old ChromaDB embeddings first (non-destructive — SQLite still intact)
        self._delete_chroma_triggers(procedure_id)

        # 2. Replace SQLite steps, triggers, and core fields
        sql.delete_procedure_steps_for(procedure_id)
        sql.delete_procedure_triggers_for(procedure_id)
        sql.update_procedure_core(procedure_id, tool, params_template, requires_variables)

        for step in steps:
            sql.save_procedure_step(
                procedure_id,
                step["step_order"],
                step["tool"],
                step["action"],
                step["params"],
                step.get("narration", ""),
                step.get("sensitive", False),
            )
        for phrase in triggers:
            sql.save_procedure_trigger(procedure_id, phrase)

        # 3. Re-embed new triggers
        proc = sql.load_procedure(procedure_id)
        name = proc["name"] if proc else procedure_id
        try:
            self._embed_triggers(procedure_id, name, triggers)
        except Exception as e:
            logger.error("[PROC] ChromaDB re-embed failed for '{}': {}", procedure_id, e)
            raise

        logger.info("[PROC] overwrite '{}' id={} new_triggers={}", name, procedure_id, len(triggers))

    # ──────────────────────────────────────────
    # Read
    # ──────────────────────────────────────────

    def load(self, procedure_id: str) -> Optional[dict]:
        """Load full procedure dict (with steps). Returns None if not found."""
        return sql.load_procedure(procedure_id)

    def list_all(self) -> list[dict]:
        """Summary list of all procedures — [{id, name, success_count, failure_count, last_used}]."""
        return sql.list_procedures()

    def find_by_name(self, name: str) -> Optional[dict]:
        """Case-insensitive exact name match. Used for 'forget X' voice commands."""
        return sql.find_procedure_by_name(name)

    # ──────────────────────────────────────────
    # Stats
    # ──────────────────────────────────────────

    def mark_success(self, procedure_id: str) -> None:
        """Record a successful replay. Resets consecutive_failures."""
        sql.update_procedure_stats(procedure_id, success=True)
        logger.debug("[PROC] mark_success id={}", procedure_id)

    def mark_failure(self, procedure_id: str) -> bool:
        """
        Record a failed replay.
        Returns True if consecutive_failures has reached the broken threshold —
        the caller should then trigger a re-learn.
        """
        consecutive = sql.update_procedure_stats(procedure_id, success=False)
        broken = consecutive >= self._broken_threshold
        logger.debug("[PROC] mark_failure id={} consecutive={} broken={}",
                     procedure_id, consecutive, broken)
        return broken

    # ──────────────────────────────────────────
    # Delete
    # ──────────────────────────────────────────

    def delete(self, procedure_id: str) -> None:
        """
        Delete a procedure from SQLite (cascade removes steps + triggers)
        and from ChromaDB.
        """
        self._delete_chroma_triggers(procedure_id)
        sql.delete_procedure(procedure_id)
        logger.info("[PROC] deleted id={}", procedure_id)

    # ──────────────────────────────────────────
    # ChromaDB helpers (internal)
    # ──────────────────────────────────────────

    def _embed_triggers(self, procedure_id: str, procedure_name: str,
                        triggers: list[str]) -> None:
        """Embed all trigger phrases and store in ChromaDB."""
        if not triggers:
            return
        col = _chroma_proc_collection()
        ids       = [_chroma_doc_id(procedure_id, i) for i in range(len(triggers))]
        documents = [t.strip().lower() for t in triggers]
        metadatas = [
            {"procedure_id": procedure_id, "phrase": t, "procedure_name": procedure_name}
            for t in documents
        ]
        col.upsert(ids=ids, documents=documents, metadatas=metadatas)
        logger.debug("[PROC] embedded {} triggers for '{}'", len(triggers), procedure_name)

    def _delete_chroma_triggers(self, procedure_id: str) -> None:
        """Delete all ChromaDB documents for a procedure."""
        try:
            col = _chroma_proc_collection()
            # ChromaDB where filter: delete all docs whose metadata.procedure_id matches
            col.delete(where={"procedure_id": procedure_id})
            logger.debug("[PROC] ChromaDB triggers deleted for id={}", procedure_id)
        except Exception as e:
            # Non-fatal — log and continue. Worst case: stale embeddings remain,
            # but they'll never match because the procedure is being deleted/overwritten.
            logger.warning("[PROC] Could not delete ChromaDB triggers for {}: {}", procedure_id, e)
