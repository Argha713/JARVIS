"""
ProcedureMatcher — semantic search for procedure triggers (Phase 4).

Answers one question: does this voice input match a saved procedure?

Uses ChromaDB cosine similarity against the procedure_triggers collection.
The embedding model follows llm.provider (OpenAI text-embedding-3-small or MiniLM).

find() is synchronous — it blocks while the embedding API call runs (~100-200ms).
TaskRouter wraps it in run_in_executor so the asyncio loop stays unblocked.
"""
import json
from pathlib import Path
from typing import Optional

from loguru import logger

from memory.procedure_service import _chroma_proc_collection
from tools.web_engine import procedure_repo

_DEFAULT_THRESHOLD    = 0.75
_DEFAULT_AMBIGUITY_GAP = 0.05


def _read_threshold() -> float:
    try:
        cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
        return float(cfg.get("procedures", {}).get("similarity_threshold", _DEFAULT_THRESHOLD))
    except Exception:
        return _DEFAULT_THRESHOLD


class ProcedureMatcher:
    """
    Instantiated once by TaskRouter. find() is called on every voice command
    before LLM routing — it must be fast and never raise.
    """

    def __init__(self):
        self._threshold     = _read_threshold()
        self._ambiguity_gap = _DEFAULT_AMBIGUITY_GAP
        logger.info("[MATCHER] threshold={} ambiguity_gap={}",
                    self._threshold, self._ambiguity_gap)

    def find(self, user_input: str) -> Optional[dict]:
        """
        Search for a procedure matching user_input.

        Returns:
          - Full procedure dict (from SQLite)  →  clear match above threshold
          - {"ambiguous": True, "candidates": [proc_a, proc_b]}  →  two close matches
          - None  →  no match, empty collection, or any error
        """
        try:
            return self._search(user_input.strip())
        except Exception as e:
            # Never crash _pre_route — log and fall through to LLM routing
            logger.warning("[MATCHER] find() error (falling through to LLM): {}", e)
            return None

    def reload_threshold(self) -> None:
        """Re-read threshold from config — call after config changes."""
        self._threshold = _read_threshold()
        logger.info("[MATCHER] threshold reloaded → {}", self._threshold)

    # ──────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────

    def _search(self, user_input: str) -> Optional[dict]:
        col = _chroma_proc_collection()

        # Nothing saved yet — skip entirely
        if col.count() == 0:
            logger.debug("[MATCHER] collection empty — no match")
            return None

        # Query top 2 so we can detect ambiguity between two close procedures
        n = min(2, col.count())
        results = col.query(query_texts=[user_input], n_results=n)

        ids       = results.get("ids",       [[]])[0]
        distances = results.get("distances", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]

        if not ids:
            logger.debug("[MATCHER] query returned no results")
            return None

        # ChromaDB returns cosine distance (0–2). Convert to similarity score.
        # score = 1 - distance  →  1.0 = identical, 0.0 = orthogonal, -1.0 = opposite
        scores = [1 - d for d in distances]
        top_score = scores[0]
        top_meta  = metadatas[0]

        logger.info("[MATCHER] top match: score={:.3f} phrase={!r} threshold={}",
                    top_score, top_meta.get("phrase", ""), self._threshold)

        # Below threshold — no match
        if top_score < self._threshold:
            logger.debug("[MATCHER] score {:.3f} below threshold {} — no match",
                         top_score, self._threshold)
            return None

        # Ambiguity check: second result also above threshold AND gap is tiny
        if len(scores) == 2 and scores[1] >= self._threshold:
            gap = top_score - scores[1]
            if gap <= self._ambiguity_gap:
                logger.info("[MATCHER] ambiguous: score_1={:.3f} score_2={:.3f} gap={:.3f}",
                            top_score, scores[1], gap)
                proc_a = procedure_repo.load_procedure(metadatas[0]["procedure_id"])
                proc_b = procedure_repo.load_procedure(metadatas[1]["procedure_id"])
                if proc_a and proc_b and proc_a["id"] != proc_b["id"]:
                    return {"ambiguous": True, "candidates": [proc_a, proc_b]}
                # Same procedure matched twice (different trigger phrases) — not ambiguous
                logger.debug("[MATCHER] ambiguous candidates resolved to same procedure — using top")

        # Clear match
        procedure_id = top_meta["procedure_id"]
        proc = procedure_repo.load_procedure(procedure_id)
        if proc is None:
            # ChromaDB has a trigger for a procedure that no longer exists in SQLite
            logger.warning("[MATCHER] stale ChromaDB trigger — procedure {} not in SQLite",
                           procedure_id)
            return None

        logger.info("[MATCHER] matched procedure '{}' id={} score={:.3f}",
                    proc["name"], proc["id"], top_score)
        return proc
