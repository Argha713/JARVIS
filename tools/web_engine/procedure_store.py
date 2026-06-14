"""
Procedure repository — SQLite CRUD for procedure memory (Phase 4).

Tables: procedures, procedure_steps, procedure_triggers
Schema is created by store.init_db() on startup — this file only handles data access.

In .NET terms: this is the IProcedureRepository implementation.
Connection management and shared helpers come from store._db / store._now.
"""
import json
import uuid
from typing import Optional

from loguru import logger

from tools.web_engine.store import _db, _now


# ─────────────────────────────────────────────
# Write operations
# ─────────────────────────────────────────────

def save_procedure(name: str, tool: str, params_template: dict,
                   requires_variables: list, narrations: dict) -> str:
    """
    Insert a new procedure row. Returns the generated procedure_id.
    Steps and triggers are saved separately via save_procedure_step / save_procedure_trigger.

    narrations: {"start": "...", "done": "...", "error": "..."}
    """
    proc_id = str(uuid.uuid4())[:8] + "_" + name.lower().replace(" ", "_")[:20]
    with _db() as con:
        con.execute("""
            INSERT INTO procedures
                (id, name, tool, params_template, created_at,
                 requires_variables, narration_start, narration_done, narration_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            proc_id,
            name,
            tool,
            json.dumps(params_template),
            _now(),
            json.dumps(requires_variables),
            narrations.get("start", ""),
            narrations.get("done", ""),
            narrations.get("error", ""),
        ))
    logger.debug("[PROC_STORE] saved id={} name={!r}", proc_id, name)
    return proc_id


def save_procedure_step(procedure_id: str, step_order: int, tool: str,
                        action: str, params: dict,
                        narration: str = "", sensitive: bool = False) -> None:
    with _db() as con:
        con.execute("""
            INSERT INTO procedure_steps
                (procedure_id, step_order, tool, action, params, narration, sensitive)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            procedure_id, step_order, tool, action,
            json.dumps(params), narration, 1 if sensitive else 0,
        ))


def save_procedure_trigger(procedure_id: str, phrase: str) -> None:
    with _db() as con:
        con.execute(
            "INSERT INTO procedure_triggers (procedure_id, phrase) VALUES (?, ?)",
            (procedure_id, phrase.strip().lower()),
        )


# ─────────────────────────────────────────────
# Read operations
# ─────────────────────────────────────────────

def load_procedure(procedure_id: str) -> Optional[dict]:
    """
    Load a procedure with its steps (guaranteed ORDER BY step_order ASC).
    Returns None if not found.
    """
    with _db() as con:
        row = con.execute(
            "SELECT * FROM procedures WHERE id=?", (procedure_id,)
        ).fetchone()
        if not row:
            return None
        steps_rows = con.execute(
            "SELECT * FROM procedure_steps WHERE procedure_id=? ORDER BY step_order ASC",
            (procedure_id,),
        ).fetchall()

    steps = [
        {
            "step_order": s["step_order"],
            "tool":       s["tool"],
            "action":     s["action"],
            "params":     json.loads(s["params"]),
            "narration":  s["narration"],
            "sensitive":  bool(s["sensitive"]),
        }
        for s in steps_rows
    ]
    return {
        "id":                   row["id"],
        "name":                 row["name"],
        "tool":                 row["tool"],
        "params_template":      json.loads(row["params_template"]),
        "created_at":           row["created_at"],
        "last_used":            row["last_used"],
        "success_count":        row["success_count"],
        "failure_count":        row["failure_count"],
        "consecutive_failures": row["consecutive_failures"],
        "requires_variables":   json.loads(row["requires_variables"]),
        "narration_start":      row["narration_start"],
        "narration_done":       row["narration_done"],
        "narration_error":      row["narration_error"],
        "steps":                steps,
    }


def list_procedures() -> list[dict]:
    """Returns [{id, name, success_count, failure_count, last_used}] for all procedures."""
    with _db() as con:
        rows = con.execute(
            "SELECT id, name, success_count, failure_count, last_used "
            "FROM procedures ORDER BY name"
        ).fetchall()
    return [
        {
            "id":            r["id"],
            "name":          r["name"],
            "success_count": r["success_count"],
            "failure_count": r["failure_count"],
            "last_used":     r["last_used"],
        }
        for r in rows
    ]


def find_procedure_by_name(name: str) -> Optional[dict]:
    """Case-insensitive exact name match. Used for forget/delete voice commands."""
    with _db() as con:
        row = con.execute(
            "SELECT id FROM procedures WHERE LOWER(name)=LOWER(?)", (name,)
        ).fetchone()
    return load_procedure(row["id"]) if row else None


def get_procedure_triggers(procedure_id: str) -> list[str]:
    """Returns all trigger phrases for a procedure."""
    with _db() as con:
        rows = con.execute(
            "SELECT phrase FROM procedure_triggers WHERE procedure_id=?",
            (procedure_id,),
        ).fetchall()
    return [r["phrase"] for r in rows]


# ─────────────────────────────────────────────
# Update operations
# ─────────────────────────────────────────────

def update_procedure_stats(procedure_id: str, success: bool) -> int:
    """
    Update success/failure counts after a replay attempt.

    On success: resets consecutive_failures to 0, updates last_used.
    On failure: increments failure_count and consecutive_failures.

    Returns the current consecutive_failures value so the caller can
    decide whether the broken threshold has been reached.
    """
    with _db() as con:
        if success:
            con.execute("""
                UPDATE procedures SET
                    success_count        = success_count + 1,
                    consecutive_failures = 0,
                    last_used            = ?
                WHERE id=?
            """, (_now(), procedure_id))
            return 0

        con.execute("""
            UPDATE procedures SET
                failure_count        = failure_count + 1,
                consecutive_failures = consecutive_failures + 1
            WHERE id=?
        """, (procedure_id,))
        row = con.execute(
            "SELECT consecutive_failures FROM procedures WHERE id=?",
            (procedure_id,),
        ).fetchone()
        return row["consecutive_failures"] if row else 0


# ─────────────────────────────────────────────
# Delete operations
# ─────────────────────────────────────────────

def delete_procedure(procedure_id: str) -> None:
    """Delete a procedure. FK cascade removes its steps and triggers automatically."""
    with _db() as con:
        con.execute("DELETE FROM procedures WHERE id=?", (procedure_id,))
    logger.debug("[PROC_STORE] deleted id={}", procedure_id)


def delete_procedure_steps_for(procedure_id: str) -> None:
    """Delete all steps for a procedure — used before re-saving steps on overwrite."""
    with _db() as con:
        con.execute(
            "DELETE FROM procedure_steps WHERE procedure_id=?", (procedure_id,)
        )


def delete_procedure_triggers_for(procedure_id: str) -> None:
    """Delete all trigger rows for a procedure — used before re-saving triggers on overwrite."""
    with _db() as con:
        con.execute(
            "DELETE FROM procedure_triggers WHERE procedure_id=?", (procedure_id,)
        )
