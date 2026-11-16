"""The one sort pipeline every front end runs: discover, extract, fit, plan, then move.

Front ends (CLI, daemon, TUI Drop Zone, demo) build an ``AppSession`` and hand it
to ``SortPipeline`` instead of wiring the extractor, analyzer and mover together
themselves. See issue #708 and the system design's "Target architecture".
"""

import inspect
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class PipelineLifecycleState(str, Enum):
    """Operational lifecycle states of SortPipeline."""

    CREATED = "created"
    PLANNING = "planning"
    PLANNED = "planned"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"

    @property
    def is_terminal(self) -> bool:
        """Return True if this is a terminal lifecycle state."""
        return self in (
            PipelineLifecycleState.COMPLETED,
            PipelineLifecycleState.FAILED,
            PipelineLifecycleState.CANCELLED,
            PipelineLifecycleState.ROLLED_BACK,
        )

    @property
    def is_active(self) -> bool:
        """Return True if the pipeline is actively performing work."""
        return self in (
            PipelineLifecycleState.PLANNING,
            PipelineLifecycleState.EXECUTING,
            PipelineLifecycleState.ROLLING_BACK,
        )


class IllegalStateTransitionError(RuntimeError, ValueError):
    """Raised when an illegal lifecycle state transition is attempted."""

    pass


_ALLOWED_TRANSITIONS: dict[PipelineLifecycleState, set[PipelineLifecycleState]] = {
    PipelineLifecycleState.CREATED: {
        PipelineLifecycleState.PLANNING,
        PipelineLifecycleState.PLANNED,
        PipelineLifecycleState.ROLLING_BACK,
    },
    PipelineLifecycleState.PLANNING: {
        PipelineLifecycleState.PLANNED,
        PipelineLifecycleState.FAILED,
        PipelineLifecycleState.CANCELLED,
    },
    PipelineLifecycleState.PLANNED: {
        PipelineLifecycleState.PLANNING,
        PipelineLifecycleState.EXECUTING,
        PipelineLifecycleState.CANCELLED,
        PipelineLifecycleState.ROLLING_BACK,
    },
    PipelineLifecycleState.EXECUTING: {
        PipelineLifecycleState.COMPLETED,
        PipelineLifecycleState.CANCELLED,
        PipelineLifecycleState.ROLLING_BACK,
        PipelineLifecycleState.FAILED,
    },
    PipelineLifecycleState.ROLLING_BACK: {
        PipelineLifecycleState.ROLLED_BACK,
        PipelineLifecycleState.FAILED,
    },
    PipelineLifecycleState.COMPLETED: {
        PipelineLifecycleState.PLANNING,
        PipelineLifecycleState.PLANNED,
        PipelineLifecycleState.ROLLING_BACK,
    },
    PipelineLifecycleState.FAILED: {
        PipelineLifecycleState.PLANNING,
        PipelineLifecycleState.PLANNED,
        PipelineLifecycleState.ROLLING_BACK,
    },
    PipelineLifecycleState.CANCELLED: {
        PipelineLifecycleState.PLANNING,
        PipelineLifecycleState.PLANNED,
        PipelineLifecycleState.ROLLING_BACK,
    },
    PipelineLifecycleState.ROLLED_BACK: {
        PipelineLifecycleState.PLANNING,
        PipelineLifecycleState.PLANNED,
        PipelineLifecycleState.ROLLING_BACK,
    },
}


def drop_scope(path: str) -> tuple[str, list | None]:
    """Return the folder to sort and the items to sort for one dropped path.

    A dropped folder is sorted whole (``None`` means discover every file in it).
    A dropped file is sorted on its own, inside its parent folder.
    """
    if os.path.isdir(path):
        return path, None
    return os.path.dirname(path), [path]


def _no_progress(info=None):
    pass


@dataclass
class SortResult:
    """What one sort or undo did, in the shape every front end reports it.

    ``moved``, ``skipped`` and ``quarantined`` list relative source paths;
    ``failed`` pairs each relative source path with its reason. ``session_id``
    names the session to undo. ``summary`` is the mover's raw summary, kept for
    JSON output and the audit report.
    """

    session_id: str | None = None
    moved: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False
    summary: dict = field(default_factory=dict)
    state: PipelineLifecycleState = PipelineLifecycleState.COMPLETED
    rollback_completed: bool | None = None

    def __post_init__(self):
        """Synchronize terminal lifecycle state and cancelled flags."""
        if self.cancelled:
            if self.state != PipelineLifecycleState.CANCELLED:
                self.state = PipelineLifecycleState.CANCELLED
        elif self.state == PipelineLifecycleState.CANCELLED:
            self.cancelled = True
        elif self.failed and self.state == PipelineLifecycleState.COMPLETED:
            self.state = PipelineLifecycleState.FAILED

    @classmethod
    def from_summary(cls, summary: dict | None, quarantine_dir: str):
        """Build a result from ``execute_moves``'s summary and its audit report."""
        summary = summary or {}
        cancelled = bool(summary.get("cancelled"))
        rollback_completed = summary.get("rollback_completed")
        state_val = summary.get("lifecycle_state")
        if state_val:
            state = (
                state_val
                if isinstance(state_val, PipelineLifecycleState)
                else PipelineLifecycleState(state_val)
            )
        elif cancelled:
            state = PipelineLifecycleState.CANCELLED
        elif summary.get("error"):
            state = PipelineLifecycleState.FAILED
        else:
            state = PipelineLifecycleState.COMPLETED

        result = cls(
            session_id=summary.get("session_id"),
            cancelled=cancelled,
            summary=summary,
            state=state,
            rollback_completed=rollback_completed,
        )
        report = summary.get("audit_report") or {}
        for rec in report.get("records") or []:
            src = rec.get("relative_source") or rec.get("source_path") or ""
            dst = (rec.get("relative_destination") or "").replace("\\", "/")
            status = rec.get("status")
            if status in ("MISMATCH_FAIL", "MOVE_FAIL"):
                result.failed.append((src, rec.get("error") or status))
            elif status == "SKIPPED":
                result.skipped.append(src)
            elif quarantine_dir in dst.split("/"):
                result.quarantined.append(src)
            else:
                result.moved.append(src)
        if result.failed and result.state == PipelineLifecycleState.COMPLETED:
            result.state = PipelineLifecycleState.FAILED
        return result

    @property
    def ok(self) -> bool:
        """True when nothing failed and the sort was not cancelled."""
        return (
            self.state
            in (
                PipelineLifecycleState.COMPLETED,
                PipelineLifecycleState.ROLLED_BACK,
            )
            and not self.failed
            and not self.cancelled
        )

    def describe(self) -> str:
        """One line for people: counts, and whether it was cancelled."""
        parts = [f"{len(self.moved)} moved"]
        if self.quarantined:
            parts.append(f"{len(self.quarantined)} quarantined")
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        line = ", ".join(parts) + "."
        return f"Cancelled: {line}" if self.cancelled else line


class SortPipeline:
    """Runs a sort over one ``AppSession``.

    The caller owns the session and closes it; the pipeline only drives it.
    """

    def __init__(self, session):
        self.session = session
        if hasattr(session, "pipeline"):
            session.pipeline = self
        initial_state = getattr(session, "lifecycle_state", None)
        if initial_state is None or not isinstance(
            initial_state, PipelineLifecycleState
        ):
            initial_state = PipelineLifecycleState.CREATED
            if hasattr(session, "lifecycle_state"):
                session.lifecycle_state = initial_state
        self._state = initial_state
        self._lock = threading.RLock()
        self._state_hooks: list[Callable] = []
        self._telemetry_hooks: list[Callable] = []
        self._rollback_completed: bool | None = None

    @property
    def state(self) -> PipelineLifecycleState:
        """The current lifecycle state of the pipeline."""
        return self._state

    @property
    def settings(self):
        """The session's settings."""
        return self.session.settings

    @property
    def base_dir(self) -> str:
        """The folder being sorted."""
        return self.session.base_dir

    def add_state_change_hook(self, callback: Callable) -> None:
        """Subscribe to lifecycle state transition notifications."""
        with self._lock:
            if callback not in self._state_hooks:
                self._state_hooks.append(callback)

    def add_telemetry_hook(self, callback: Callable) -> None:
        """Subscribe to pipeline telemetry event notifications."""
        with self._lock:
            if callback not in self._telemetry_hooks:
                self._telemetry_hooks.append(callback)

    def _validate_transition(self, target_state: PipelineLifecycleState) -> None:
        allowed = _ALLOWED_TRANSITIONS.get(self._state, set())
        if target_state not in allowed:
            raise IllegalStateTransitionError(
                f"Illegal pipeline state transition from '{self._state.value}' to '{target_state.value}'."
            )

    def transition_to(
        self,
        target_state: PipelineLifecycleState,
        context: dict | None = None,
    ) -> None:
        """Validate and synchronously transition to a new lifecycle state."""
        with self._lock:
            if target_state == self._state:
                return
            self._validate_transition(target_state)
            old_state = self._state
            self._state = target_state
            if hasattr(self.session, "lifecycle_state"):
                self.session.lifecycle_state = target_state

        self._notify_state_change(old_state, target_state, context)

    def _notify_state_change(
        self,
        old_state: PipelineLifecycleState,
        new_state: PipelineLifecycleState,
        context: dict | None = None,
    ) -> None:
        hooks = list(self._state_hooks)
        for hook in hooks:
            try:
                sig = inspect.signature(hook)
                param_count = len(
                    [
                        p
                        for p in sig.parameters.values()
                        if p.kind
                        in (
                            inspect.Parameter.POSITIONAL_ONLY,
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        )
                    ]
                )
                if param_count == 1:
                    hook(new_state)
                elif param_count == 2:
                    hook(old_state, new_state)
                else:
                    hook(old_state, new_state, context or {})
            except Exception as e:
                logger.warning(f"Error in state change hook {hook}: {e}", exc_info=True)

    def emit_telemetry(
        self, event: str, data: dict | None = None, **kwargs: Any
    ) -> None:
        """Safely broadcast telemetry event to registered subscribers."""
        payload = dict(data or {})
        payload.update(kwargs)
        payload["state"] = self.state.value
        hooks = list(self._telemetry_hooks)
        for hook in hooks:
            try:
                hook(event, payload)
            except Exception as e:
                logger.warning(f"Error in telemetry hook {hook}: {e}", exc_info=True)

    def plan(
        self,
        items: list | None = None,
        *,
        progress_callback: Callable | None = None,
        cancel_check: Callable | None = None,
        chunk_size: int = 50,
    ) -> Any:
        """Extract text, fit the analyzer and return the plan. Nothing moves.

        ``items`` defaults to every file under the session's source folder.
        """
        if self.state in (
            PipelineLifecycleState.EXECUTING,
            PipelineLifecycleState.ROLLING_BACK,
        ):
            raise IllegalStateTransitionError(
                f"Cannot plan pipeline while execution or rollback is in progress (current state: '{self.state.value}')."
            )
        if self.state == PipelineLifecycleState.PLANNING:
            raise IllegalStateTransitionError("Planning is already in progress.")

        self.transition_to(PipelineLifecycleState.PLANNING)
        self.emit_telemetry("plan_start", items=items)

        try:
            from app.core.extractor import build_corpus_generator

            if items is None:
                from app.core.scanner import get_files_recursively

                items = get_files_recursively(self.base_dir)

            generator = build_corpus_generator(
                base_dir=self.base_dir,
                items_to_sort=items,
                progress_callback=progress_callback or _no_progress,
                max_workers=getattr(self.settings, "MAX_WORKERS", 2),
                db=self.session.db,
                chunk_size=chunk_size,
                cancel_check=cancel_check,
                settings=self.settings,
            )
            for chunk in generator:
                if cancel_check and cancel_check():
                    self.transition_to(PipelineLifecycleState.CANCELLED)
                    self.emit_telemetry("plan_cancelled")
                    return None
                self.session.partial_fit(chunk)

            if cancel_check and cancel_check():
                self.transition_to(PipelineLifecycleState.CANCELLED)
                self.emit_telemetry("plan_cancelled")
                return None

            plan = self.session.generate_sorting_plan()
            self.transition_to(PipelineLifecycleState.PLANNED)
            self.emit_telemetry("plan_finish", plan=plan)
            return plan
        except Exception as e:
            if self.state != PipelineLifecycleState.FAILED:
                try:
                    self.transition_to(PipelineLifecycleState.FAILED)
                except IllegalStateTransitionError as trans_err:
                    logger.debug(f"Failed to transition to FAILED state: {trans_err}")
            self.emit_telemetry("plan_error", error=str(e))
            raise

    def execute(self, plan, **kwargs) -> SortResult:
        """Move the files in ``plan`` and return what happened."""
        if self.state != PipelineLifecycleState.PLANNED:
            raise IllegalStateTransitionError(
                f"Cannot execute pipeline in state '{self.state.value}'; pipeline must be in 'planned' state."
            )
        self.transition_to(PipelineLifecycleState.EXECUTING)
        self.emit_telemetry("execute_start", plan=plan)

        call_kwargs = dict(kwargs)
        try:
            fn = (
                getattr(self.session.execute_moves, "side_effect", None)
                or self.session.execute_moves
            )
            sig = inspect.signature(fn)
            accepts_pipeline = "pipeline" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
        except (ValueError, TypeError):
            accepts_pipeline = True

        if accepts_pipeline:
            call_kwargs["pipeline"] = self

        try:
            summary = self.session.execute_moves(plan, **call_kwargs)
            quarantine_dir = getattr(
                self.settings, "QUARANTINE_DIR_NAME", "_Compliance_Quarantine"
            )
            result = SortResult.from_summary(summary, quarantine_dir)
            if result.cancelled or summary.get("cancelled"):
                if self.state != PipelineLifecycleState.CANCELLED:
                    self.transition_to(PipelineLifecycleState.CANCELLED)
                result.state = PipelineLifecycleState.CANCELLED
            elif result.failed or not result.ok:
                if self.state != PipelineLifecycleState.FAILED:
                    self.transition_to(PipelineLifecycleState.FAILED)
                result.state = PipelineLifecycleState.FAILED
            else:
                if self.state != PipelineLifecycleState.COMPLETED:
                    self.transition_to(PipelineLifecycleState.COMPLETED)
                result.state = PipelineLifecycleState.COMPLETED

            if hasattr(self, "_rollback_completed"):
                result.rollback_completed = self._rollback_completed

            self.emit_telemetry("execute_finish", result=result)
            return result
        except Exception as e:
            if self.state != PipelineLifecycleState.FAILED:
                if self.state == PipelineLifecycleState.EXECUTING:
                    try:
                        self.transition_to(PipelineLifecycleState.ROLLING_BACK)
                    except IllegalStateTransitionError as trans_err:
                        logger.debug(
                            f"Failed to transition to ROLLING_BACK state: {trans_err}"
                        )
                try:
                    self.transition_to(PipelineLifecycleState.FAILED)
                except IllegalStateTransitionError as trans_err:
                    logger.debug(f"Failed to transition to FAILED state: {trans_err}")
            self.emit_telemetry("execute_error", error=str(e))
            raise

    def undo(
        self,
        session_id: str,
        *,
        ignore_missing: bool = False,
        selected_step_ids: Any | None = None,
    ) -> SortResult:
        """Move a past sort's files back and return what happened (ADR 0006).

        ``moved`` lists each file put back where it came from, and ``failed``
        each one left in place, such as a file changed since the sort. Raises
        ``ValueError`` for an unknown session.
        """
        if self.state in (
            PipelineLifecycleState.PLANNING,
            PipelineLifecycleState.EXECUTING,
            PipelineLifecycleState.ROLLING_BACK,
        ):
            raise IllegalStateTransitionError(
                f"Cannot undo while operation is in progress (current state: '{self.state.value}')."
            )

        self.transition_to(PipelineLifecycleState.ROLLING_BACK)
        self.emit_telemetry("undo_start", session_id=session_id)

        from app.core.moves import FAILED, INTENT
        from app.core.path_utils import safe_relpath

        history = self.session.history_manager
        base_dir = next(
            (
                s["base_dir"]
                for s in history.get_sessions()
                if s["session_id"] == session_id
            ),
            None,
        )
        if base_dir is None:
            self.transition_to(PipelineLifecycleState.FAILED)
            raise ValueError(f"Session not found: {session_id}")
        moves = [
            m
            for m in history.moves.list_moves(session_id)
            if m.state not in (INTENT, FAILED)
        ]
        if selected_step_ids is not None:
            sel_set = {str(x) for x in selected_step_ids}
            moves = [m for m in moves if str(m.seq) in sel_set]

        result = SortResult(
            session_id=session_id, state=PipelineLifecycleState.ROLLING_BACK
        )
        reason = "Not moved back"
        rollback_succeeded = False
        try:
            kwargs = {"ignore_missing": ignore_missing}
            if selected_step_ids is not None:
                kwargs["selected_step_ids"] = selected_step_ids
            result.summary["restored"] = history.rollback(session_id, **kwargs)
            rollback_succeeded = True
        except (OSError, ValueError) as err:
            # Undo still moves back every file it can before it reports the rest.
            reason = str(err)
            result.summary["error"] = reason
            rollback_succeeded = False
        for move in moves:
            src = safe_relpath(move.src, base_dir).replace("\\", "/")
            if os.path.lexists(move.src):
                result.moved.append(src)
            else:
                result.failed.append((src, reason))
        if "error" in result.summary and not result.failed:
            result.failed.append((session_id, reason))

        result.rollback_completed = rollback_succeeded and not result.failed
        if result.rollback_completed:
            self.transition_to(PipelineLifecycleState.ROLLED_BACK)
            result.state = PipelineLifecycleState.ROLLED_BACK
        else:
            self.transition_to(PipelineLifecycleState.FAILED)
            result.state = PipelineLifecycleState.FAILED

        self.emit_telemetry("undo_finish", result=result)
        return result

    def run(self, items: list | None = None, *, dry_run: bool = False, **kwargs):
        """Plan, then execute unless ``dry_run``. Returns ``(plan, result)``.

        A dry run's result is ``None``.
        """
        plan = self.plan(items, **kwargs)
        if dry_run:
            return plan, None
        return plan, self.execute(plan)
