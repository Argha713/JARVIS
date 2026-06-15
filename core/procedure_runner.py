"""
ProcedureRunner — replays a saved procedure (Phase 4).

Makes ONE call to tool_registry.run() with the procedure's params_template,
after substituting all {variable} placeholders with resolved values.

Does NOT iterate or execute individual procedure_steps rows — those are stored
for monitoring and debugging only. The single tool call goes through WebEngine
exactly as the first execution did, so session management, API-first path,
and all WebEngine internals still apply.

Sync — TaskRouter calls it via run_in_executor, same as all other tools.
"""
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

_CONFIG_PATH          = "config.json"
_DOUBLE_SUBMIT_KEY    = "double_submit_guard_sec"
_DEFAULT_GUARD_SEC    = 30
_PASSTHROUGH_PREFIX   = "__PASSTHROUGH__:"


def _read_config() -> dict:
    try:
        return json.loads(Path(_CONFIG_PATH).read_text(encoding="utf-8"))
    except Exception:
        return {}


class ProcedureRunner:
    """
    Instantiated once by TaskRouter.
    run() is synchronous — wrap in run_in_executor from async context.
    """

    def __init__(self, tool_registry, narration, procedure_service=None):
        self._tools     = tool_registry
        self._narration = narration
        self._svc       = procedure_service   # optional — for mark_success/failure

    # ──────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────

    def run(self, procedure: dict, resolved_vars: dict) -> str:
        """
        Replay a procedure.

        procedure:     full procedure dict from ProcedureService.load()
        resolved_vars: {"tasks_done": "fixed login bug", "today": "2026-06-14"}
                       TaskRouter extracts these before calling run().

        Returns the tool result string (may be a __PASSTHROUGH__ response).
        Raises ProcedureExecutionError on tool failure.
        """
        name = procedure.get("name", "procedure")
        pid  = procedure.get("id", "")

        # Narrate start
        start_msg = procedure.get("narration_start", "")
        if start_msg:
            self._narration.step(start_msg)

        # Resolve all {var} placeholders in params_template
        params = self._resolve_params(procedure.get("params_template", {}), resolved_vars)
        tool   = procedure.get("tool", "web")

        logger.info("[RUNNER] Replaying '{}' id={} tool={} params={}", name, pid, tool, params)

        try:
            result = self._tools.run(tool, params)
        except Exception as e:
            logger.error("[RUNNER] '{}' execution failed: {}", name, e)
            error_msg = procedure.get("narration_error", "")
            if error_msg:
                self._narration.step(error_msg)
            if self._svc and pid:
                broken = self._svc.mark_failure(pid)
                if broken:
                    logger.warning("[RUNNER] '{}' hit broken threshold — re-learn needed", name)
                    return (
                        f"{_PASSTHROUGH_PREFIX}The saved steps for {name} seem outdated. "
                        f"Let me try fresh, sir."
                    )
            raise ProcedureExecutionError(name, e) from e

        # Success
        if self._svc and pid:
            self._svc.mark_success(pid)

        done_msg = procedure.get("narration_done", "")
        if done_msg:
            self._narration.step(done_msg)

        logger.info("[RUNNER] '{}' completed successfully", name)
        return result

    def would_double_submit(self, procedure: dict) -> bool:
        """
        Returns True if this procedure was last used within the double-submit
        guard window. TaskRouter calls this before run() and asks the user to
        confirm before proceeding.
        """
        last_used = procedure.get("last_used")
        if not last_used:
            return False
        try:
            cfg       = _read_config()
            guard_sec = cfg.get("procedures", {}).get(_DOUBLE_SUBMIT_KEY, _DEFAULT_GUARD_SEC)
            from datetime import timezone
            last_dt   = datetime.fromisoformat(last_used)
            now       = datetime.now(last_dt.tzinfo or timezone.utc)
            elapsed   = (now - last_dt).total_seconds()
            if elapsed < guard_sec:
                logger.info("[RUNNER] double-submit guard: last_used {:.0f}s ago (guard={}s)",
                            elapsed, guard_sec)
                return True
        except Exception as e:
            logger.debug("[RUNNER] double-submit check failed (non-fatal): {}", e)
        return False

    # ──────────────────────────────────────────
    # Variable resolution
    # ──────────────────────────────────────────

    def _resolve_params(self, template: dict, resolved_vars: dict) -> dict:
        """
        Deep-copy template, replacing every {var_name} placeholder in string
        values with the corresponding value from resolved_vars.

        Built-in variables (auto-resolved if not in resolved_vars):
          {today}     → current date as YYYY-MM-DD
          {user_name} → from config.json jarvis.user_name, fallback "sir"
        """
        builtins = self._build_builtins()
        all_vars = {**builtins, **resolved_vars}   # resolved_vars wins over builtins
        return self._substitute(template, all_vars)

    @staticmethod
    def _build_builtins() -> dict:
        builtins = {
            "today": datetime.now().strftime("%Y-%m-%d"),
        }
        try:
            cfg = _read_config()
            builtins["user_name"] = cfg.get("jarvis", {}).get("user_name", "sir")
        except Exception:
            builtins["user_name"] = "sir"
        return builtins

    @staticmethod
    def _substitute(obj, vars_map: dict):
        """Recursively walk obj and replace {key} placeholders in strings."""
        if isinstance(obj, str):
            def replacer(match):
                key = match.group(1)
                if key in vars_map and vars_map[key] is not None:
                    return str(vars_map[key])
                logger.warning("[RUNNER] unresolved variable {{{}}}", key)
                return match.group(0)   # leave placeholder intact
            return re.sub(r"\{(\w+)\}", replacer, obj)
        if isinstance(obj, dict):
            return {k: ProcedureRunner._substitute(v, vars_map) for k, v in obj.items()}
        if isinstance(obj, list):
            return [ProcedureRunner._substitute(item, vars_map) for item in obj]
        return obj


class ProcedureExecutionError(Exception):
    """Raised by ProcedureRunner.run() when the underlying tool call fails."""
    def __init__(self, procedure_name: str, cause: Exception):
        self.procedure_name = procedure_name
        self.cause          = cause
        super().__init__(f"Procedure '{procedure_name}' failed: {cause}")
