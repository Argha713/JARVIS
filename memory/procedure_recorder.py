"""
ProcedureRecorder — passive step logger used during tool execution.

Created fresh for each voice command cycle by TaskRouter.
Passed into the tool via params["__recorder__"].
The tool writes steps into it; TaskRouter reads the result after execution.

Checkpoint model (like a DB savepoint):
  - Steps accumulate in a "pending" buffer
  - checkpoint() promotes pending → confirmed, clears pending
  - discard() wipes everything (called on any exception)
  - commit() finalises and returns the confirmed step list

This means failed navigation attempts (before checkpoint) are automatically
discarded — only steps after a confirmed navigation are saved.

Thread safety: tools run in a thread executor; TaskRouter (async) reads after
the executor returns. They never overlap, but Lock is here for future safety.
"""
import threading
from loguru import logger


class ProcedureRecorder:

    def __init__(self):
        self._lock       = threading.Lock()
        self._confirmed  = []   # steps that passed a checkpoint
        self._pending    = []   # steps since last checkpoint (not yet confirmed)
        self._variables  = {}   # param_key -> var_name for last step
        self._req_vars   = set()  # all variable names needed across all steps
        self._name_hint  = ""
        self._committed  = False

    # ──────────────────────────────────────────
    # Tool-facing API
    # ──────────────────────────────────────────

    def begin(self, name_hint: str = "") -> None:
        """Call at the very start of tool execution. Resets any previous state."""
        with self._lock:
            self._confirmed  = []
            self._pending    = []
            self._variables  = {}
            self._req_vars   = set()
            self._name_hint  = name_hint
            self._committed  = False
        logger.debug("[RECORDER] begin name_hint={!r}", name_hint)

    def step(self, tool: str, action: str, params: dict,
             narration: str = "", sensitive: bool = False) -> None:
        """
        Record one replayable step into the pending buffer.

        tool:      "web" | "browser" | "web_search"
        action:    "browser_open" | "form_fill" | "click_submit" | "read_content"
        params:    dict — may contain {variable} placeholders after mark_variable()
        sensitive: if True, params hold a credential pointer, not a raw value
        """
        step = {
            "tool":      tool,
            "action":    action,
            "params":    dict(params),   # shallow copy — don't hold a reference to caller's dict
            "narration": narration,
            "sensitive": sensitive,
        }
        with self._lock:
            self._pending.append(step)
        logger.debug("[RECORDER] step {} {} params={}", tool, action, params)

    def mark_variable(self, param_key: str, var_name: str, source: str = "speech") -> None:
        """
        Mark a param in the most recently recorded step as a runtime variable.

        param_key: the key inside the last step's params dict
        var_name:  the placeholder name, e.g. "tasks_done"
        source:    "speech" | "clock" | "config"

        After this call, params[param_key] is replaced with "{var_name}" so that
        ProcedureRunner can substitute the real value at replay time.
        """
        with self._lock:
            target = self._pending if self._pending else self._confirmed
            if not target:
                logger.warning("[RECORDER] mark_variable called with no recorded steps")
                return
            last_step = target[-1]
            if param_key in last_step["params"]:
                last_step["params"][param_key] = "{" + var_name + "}"
            self._req_vars.add(var_name)
        logger.debug("[RECORDER] mark_variable {}={} source={}", param_key, var_name, source)

    def checkpoint(self) -> None:
        """
        Promote all pending steps to confirmed. Clears the pending buffer.

        Call this only after the tool has confirmed it reached the right state
        (e.g. the target page loaded, the section was found). Any failed
        navigation attempts recorded before this point are silently dropped.
        """
        with self._lock:
            count = len(self._pending)
            self._confirmed.extend(self._pending)
            self._pending = []
        logger.debug("[RECORDER] checkpoint: promoted {} pending steps (confirmed={})",
                     count, len(self._confirmed))

    def commit(self) -> list[dict]:
        """
        Finalise recording. Returns the clean confirmed step list.
        Call only on full tool success. Recorder is frozen after this.
        Pending steps (after last checkpoint) are discarded.
        """
        with self._lock:
            if self._pending:
                logger.debug("[RECORDER] commit: discarding {} unconfirmed trailing steps",
                             len(self._pending))
            self._pending   = []
            self._committed = True
            steps = [self._numbered(s, i) for i, s in enumerate(self._confirmed, start=1)]
        logger.debug("[RECORDER] commit: {} steps finalised", len(steps))
        return steps

    def discard_pending(self) -> None:
        """
        Clear only the pending buffer, keep confirmed steps intact.

        Use when a single navigation attempt failed and the tool is about to
        try another route — the confirmed steps so far are still valid.
        Example: discoverer tried link A (stepped, pending), it was the wrong page,
        so discard_pending() and try link B.
        """
        with self._lock:
            dropped = len(self._pending)
            self._pending = []
        logger.debug("[RECORDER] discard_pending: dropped {} unconfirmed steps", dropped)

    def discard(self) -> None:
        """
        Full failure — wipe everything (confirmed + pending). Call in exception handlers.
        Prevents saving a procedure that didn't fully succeed.
        """
        with self._lock:
            dropped = len(self._confirmed) + len(self._pending)
            self._confirmed = []
            self._pending   = []
            self._committed = False
        logger.debug("[RECORDER] discard: dropped {} steps (confirmed+pending)", dropped)

    # ──────────────────────────────────────────
    # TaskRouter-facing API (read after executor returns)
    # ──────────────────────────────────────────

    def step_count(self) -> int:
        """Number of confirmed steps (does not count pending)."""
        with self._lock:
            return len(self._confirmed)

    def get_requires_variables(self) -> list[str]:
        """Sorted list of variable names needed across all confirmed steps."""
        with self._lock:
            return sorted(self._req_vars)

    @property
    def name_hint(self) -> str:
        """The hint passed to begin(), used by LLM when naming the procedure."""
        return self._name_hint

    # ──────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────

    @staticmethod
    def _numbered(step: dict, order: int) -> dict:
        """Add step_order to the step dict so it's ready for save_procedure_step()."""
        return {**step, "step_order": order}
