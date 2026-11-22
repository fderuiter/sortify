"""The one sort pipeline every front end runs: discover, extract, fit, plan, then move.

Front ends (CLI, daemon, TUI Drop Zone, demo) build an ``AppSession`` and hand it
to ``SortPipeline`` instead of wiring the extractor, analyzer and mover together
themselves. See issue #708 and the system design's "Target architecture".
"""

import inspect
import logging
import os
import threading
from collections.abc import Callable, Collection, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from app.core.lifecycle import LifecycleTelemetryMixin

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


@dataclass(frozen=True)
class DropPathIssue:
    """Structured validation issue for a rejected drop target path."""

    raw_path: str
    clean_path: str
    abs_path: str
    reason: str


def validate_drop_paths(
    paths: Sequence[Any],
    *,
    settings: Any = None,
    ignore_predicate: Callable[[str], bool] | None = None,
) -> tuple[list[str], list[DropPathIssue]]:
    """Validate dropped target paths against existence, ignore, and protection rules."""
    from app.core.scanner import ScanFilter

    validated_paths: list[str] = []
    issues: list[DropPathIssue] = []
    scan_filter = ScanFilter.from_settings(settings=settings)

    for raw_path in paths:
        if not raw_path or not isinstance(raw_path, str):
            continue
        clean_path = raw_path.strip().strip("'\"")
        if not clean_path:
            continue
        abs_path = os.path.abspath(clean_path)
        if not os.path.exists(abs_path):
            issues.append(DropPathIssue(raw_path, clean_path, abs_path, "missing"))
            continue
        if scan_filter.is_protected(abs_path):
            issues.append(DropPathIssue(raw_path, clean_path, abs_path, "protected"))
            continue
        if ignore_predicate is not None and ignore_predicate(abs_path):
            issues.append(DropPathIssue(raw_path, clean_path, abs_path, "ignored"))
            continue
        if (
            scan_filter.is_app_bundle(abs_path)
            or scan_filter.has_ignored_extension(abs_path)
            or scan_filter.is_quarantine(abs_path)
        ):
            issues.append(DropPathIssue(raw_path, clean_path, abs_path, "ignored"))
            continue
        validated_paths.append(abs_path)

    return validated_paths, issues


def _bind_plan_sources(
    node: Any,
    source_dir: str,
    active_parent_path: str = "",
    depth: int = 0,
) -> Any:
    """Recursively copy plan nodes and bind resolved ``source_path`` before re-keying."""
    from app.core.domain_contracts import _get_val
    from app.core.mover import _resolve_source_path

    curr = node.plan if hasattr(node, "plan") and isinstance(node.plan, dict) else node
    if not isinstance(curr, dict):
        return node

    bound: dict[str, Any] = {}
    for key, content in curr.items():
        if str(key).startswith("__"):
            bound[key] = content
            continue
        node_type = _get_val(content, "node_type") or _get_val(content, "__type__")
        is_file_leaf = (
            content is None
            or node_type == "file"
            or (
                isinstance(content, dict)
                and node_type != "directory"
                and (
                    "relative_source" in content
                    or "target_filename" in content
                    or "routed_by" in content
                )
            )
        )
        if is_file_leaf:
            eff_source_dir = source_dir or str(_get_val(content, "source_root") or "")
            if content is None:
                if eff_source_dir:
                    resolved = _resolve_source_path(
                        eff_source_dir, key, content, active_parent_path, depth
                    )
                    bound[key] = {
                        "__type__": "file",
                        "relative_source": key,
                        "source_root": eff_source_dir,
                        "source_path": resolved,
                    }
                else:
                    bound[key] = None
            else:
                copied: dict[str, Any] = (
                    content.model_dump(by_alias=True)
                    if hasattr(content, "model_dump") and not isinstance(content, dict)
                    else dict(content)
                )
                existing_sp = copied.get("source_path")
                if existing_sp and os.path.isabs(str(existing_sp)):
                    resolved_sp: str | None = os.path.normpath(str(existing_sp))
                elif eff_source_dir:
                    resolved_sp = _resolve_source_path(
                        eff_source_dir, key, copied, active_parent_path, depth
                    )
                    if not os.path.lexists(resolved_sp):
                        candidate_by_key = os.path.normpath(
                            os.path.join(eff_source_dir, str(key))
                        )
                        if os.path.lexists(candidate_by_key):
                            resolved_sp = candidate_by_key
                else:
                    resolved_sp = None
                if resolved_sp:
                    copied["source_path"] = resolved_sp
                if eff_source_dir and not copied.get("source_root"):
                    copied["source_root"] = eff_source_dir
                bound[key] = copied
        elif isinstance(content, dict):
            next_parent = (
                active_parent_path
                if os.path.isabs(key)
                else (
                    os.path.join(active_parent_path, key) if active_parent_path else key
                )
            )
            bound[key] = _bind_plan_sources(content, source_dir, next_parent, depth + 1)
        else:
            bound[key] = content
    return bound


def reroot_plan(
    plan: Any, dest_dir: str, source_dir: str | None = None
) -> dict[str, Any]:
    """Re-key a plan's relative entries under ``dest_dir`` and bind ``source_path``."""
    from app.core.domain_contracts import _get_val
    from app.core.scanner import discover_existing_folders

    dest_base = Path(dest_dir).resolve()
    dest_base.mkdir(parents=True, exist_ok=True)
    existing_map = {
        name.lower(): name
        for name in discover_existing_folders(None, dest_dir=str(dest_base))
    }
    bound_plan = _bind_plan_sources(plan, source_dir or "")
    plan_items = (
        bound_plan.plan.items()
        if hasattr(bound_plan, "plan") and isinstance(bound_plan.plan, dict)
        else bound_plan.items()
    )
    re_rooted_plan: dict[str, Any] = {}
    for key, value in plan_items:
        node_type = _get_val(value, "node_type") or _get_val(value, "__type__")
        is_root_file = value is None or node_type == "file"
        if os.path.isabs(key):
            re_rooted_plan[key] = value
        elif is_root_file:
            if isinstance(value, dict) and not value.get("destination_override"):
                file_val = dict(value)
                file_val["destination_override"] = str(dest_base)
                re_rooted_plan[key] = file_val
            else:
                re_rooted_plan[key] = value
        else:
            parts = str(key).replace("\\", "/").strip("/").split("/")
            if parts and parts[0].lower() in existing_map:
                parts[0] = existing_map[parts[0].lower()]
                resolved_rel = "/".join(parts)
            else:
                resolved_rel = key
            re_rooted_plan[str(dest_base / resolved_rel)] = value
    return re_rooted_plan


def _no_progress(info=None):
    pass


def _relativize_items_to_base(items: Sequence[Any], base_dir: str) -> list[Any]:
    """Convert absolute file items inside ``base_dir`` to relative paths.

    ``drop_scope(file_path)`` returns ``(parent_dir, [abs_file_path])`` while
    extraction and plan hierarchy injection expect paths relative to
    ``base_dir`` unless namespaced with ``::``. Relative items, ``::``
    identifiers, and paths outside ``base_dir`` are preserved unchanged.
    """
    import ntpath

    from app.core.mover import is_subpath_or_equal
    from app.core.path_utils import safe_relpath

    if not base_dir:
        return list(items)

    normalized: list[Any] = []
    for item in items:
        if not isinstance(item, str) or not item or "::" in item:
            normalized.append(item)
            continue

        if os.path.isabs(item) and os.path.isabs(base_dir):
            if is_subpath_or_equal(item, base_dir):
                rel = safe_relpath(item, base_dir)
                if (
                    rel
                    and rel != "."
                    and not rel.startswith("..")
                    and not os.path.isabs(rel)
                ):
                    normalized.append(rel)
                    continue
        elif ntpath.isabs(item) and ntpath.isabs(base_dir):
            norm_item = ntpath.normcase(ntpath.normpath(item))
            norm_base = ntpath.normcase(ntpath.normpath(base_dir)).rstrip("\\")
            if norm_item != norm_base and norm_item.startswith(norm_base + "\\"):
                rel = ntpath.relpath(ntpath.normpath(item), ntpath.normpath(base_dir))
                if (
                    rel
                    and rel != "."
                    and not rel.startswith("..")
                    and not ntpath.isabs(rel)
                ):
                    normalized.append(rel)
                    continue

        normalized.append(item)
    return normalized


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


class SortPipeline(LifecycleTelemetryMixin):
    """Runs a sort over one ``AppSession``.

    The caller owns the session and closes it; the pipeline only drives it.
    """

    def __init__(
        self,
        session,
        *,
        base_dir: str | None = None,
        settings: Any = None,
    ):
        self.session = session
        self._base_dir = base_dir
        self._settings = settings
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
        self._rollback_completed: bool | None = None

    @property
    def state(self) -> PipelineLifecycleState:
        """The current lifecycle state of the pipeline."""
        return self._state

    @property
    def settings(self):
        """The session's settings."""
        if self._settings is not None:
            return self._settings
        return getattr(self.session, "settings", None)

    @property
    def base_dir(self) -> str:
        """The folder being sorted."""
        if self._base_dir is not None:
            return self._base_dir
        return getattr(self.session, "base_dir", "")

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
        """Broadcast lifecycle state transition via LifecycleTelemetryMixin."""
        self._notify_lifecycle_listeners(old_state, new_state, context=context)

    def plan(
        self,
        items: list | None = None,
        *,
        progress_callback: Callable | None = None,
        cancel_check: Callable | None = None,
        chunk_size: int = 50,
        fast_path_only: bool | None = None,
        target_paths: list[str] | None = None,
        jev_results: dict[str, Any] | None = None,
        run_metadata_pass: bool = True,
        bypassed_items: Collection[str] | None = None,
        dest_dir: str | None = None,
        existing_folders: list[str] | None = None,
    ) -> Any:
        """Extract text, fit the analyzer and return the plan. Nothing moves.

        ``items`` defaults to every file under the session's source folder.
        Supports deterministic fast-path planning, ``MetadataPass`` pre-filtering,
        Jev fast-path overrides, and targeted single-path scoping.
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

        if hasattr(self.session, "pipeline"):
            self.session.pipeline = self
        self.transition_to(PipelineLifecycleState.PLANNING)
        self.emit_telemetry("plan_start", items=items)

        try:
            from app.core.scanner import (
                ScanFilter,
                discover_existing_folders,
                get_files_recursively,
                parse_item_identifier,
            )

            scan_filter = ScanFilter.from_settings(
                settings=self.settings, base_dir=self.base_dir
            )
            if items is None:
                items = get_files_recursively(
                    self.base_dir,
                    settings=self.settings,
                    scan_filter=scan_filter,
                )
            else:
                if self.base_dir:
                    items = _relativize_items_to_base(items, self.base_dir)
                filtered_items = []
                for it in items:
                    src_root, rel_p = parse_item_identifier(
                        str(it), default_base_dir=self.base_dir or ""
                    )
                    cand_abs = os.path.join(src_root, rel_p) if src_root else str(it)
                    if (
                        scan_filter.check_path(
                            cand_abs,
                            base_dir=src_root or self.base_dir,
                            check_depth=False,
                            check_hidden=False,
                        )
                        is None
                    ):
                        filtered_items.append(it)
                items = filtered_items

            metadata_bypassed: list[Any] = []
            if (
                run_metadata_pass
                and items
                and getattr(self.session, "db", None) is not None
            ):
                from app.core.metadata import MetadataPass

                metadata_bypassed = (
                    MetadataPass.run(
                        self.base_dir,
                        items,
                        self.settings,
                        self.session.db,
                        progress_callback,
                        cancel_check,
                        source_dirs=getattr(self.session, "source_dirs", None),
                    )
                    or []
                )
                if cancel_check and cancel_check():
                    self.transition_to(PipelineLifecycleState.CANCELLED)
                    self.emit_telemetry("plan_cancelled")
                    return None

            if not bool(fast_path_only):
                from app.core.extractor import build_corpus_generator

                bypassed_set = {
                    str(f).replace("\\", "/") for f in metadata_bypassed
                } | {str(d).replace("\\", "/") for d in (bypassed_items or ())}
                items_to_extract = (
                    [f for f in items if str(f).replace("\\", "/") not in bypassed_set]
                    if bypassed_set
                    else items
                )

                if items_to_extract:
                    raw_workers = getattr(self.settings, "MAX_WORKERS", 2)
                    max_workers = (
                        raw_workers
                        if isinstance(raw_workers, int) and raw_workers > 0
                        else 2
                    )
                    generator = build_corpus_generator(
                        base_dir=self.base_dir,
                        items_to_sort=items_to_extract,
                        progress_callback=progress_callback or _no_progress,
                        max_workers=max_workers,
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

            plan_kwargs: dict[str, Any] = {}
            if fast_path_only is not None:
                plan_kwargs["fast_path_only"] = bool(fast_path_only)
            if target_paths is not None:
                plan_kwargs["target_paths"] = target_paths
            if jev_results is not None:
                plan_kwargs["jev_results"] = jev_results

            resolved_existing = (
                list(existing_folders)
                if existing_folders is not None
                else discover_existing_folders(
                    self.base_dir, dest_dir=dest_dir, settings=self.settings
                )
            )
            try:
                gen_sig = inspect.signature(self.session.generate_sorting_plan)
                has_var_kw = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD
                    for p in gen_sig.parameters.values()
                )
                if resolved_existing and (
                    has_var_kw or "existing_folders" in gen_sig.parameters
                ):
                    plan_kwargs["existing_folders"] = resolved_existing
                if dest_dir and (has_var_kw or "dest_dir" in gen_sig.parameters):
                    plan_kwargs["dest_dir"] = dest_dir
            except (ValueError, TypeError):
                if resolved_existing:
                    plan_kwargs["existing_folders"] = resolved_existing
                if dest_dir:
                    plan_kwargs["dest_dir"] = dest_dir

            plan = self.session.generate_sorting_plan(**plan_kwargs)
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

    def execute(
        self,
        plan,
        *,
        move_context: Callable[[Any], AbstractContextManager[Any]] | None = None,
        **kwargs,
    ) -> SortResult:
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
            accepts_pipeline = "pipeline" in sig.parameters
        except (ValueError, TypeError):
            accepts_pipeline = True

        if accepts_pipeline:
            call_kwargs["pipeline"] = self

        try:
            ctx = move_context(plan) if move_context is not None else nullcontext()
            with ctx:
                summary = self.session.execute_moves(plan, **call_kwargs)
            raw_quarantine_dir = getattr(
                self.settings, "QUARANTINE_DIR_NAME", "_Compliance_Quarantine"
            )
            quarantine_dir = (
                raw_quarantine_dir
                if isinstance(raw_quarantine_dir, str)
                else "_Compliance_Quarantine"
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

    def run(
        self,
        items: list | None = None,
        *,
        dry_run: bool = False,
        move_context: Callable[[Any], AbstractContextManager[Any]] | None = None,
        **kwargs,
    ):
        """Plan, then execute unless ``dry_run``. Returns ``(plan, result)``.

        A dry run's result is ``None``.
        """
        plan = self.plan(items, **kwargs)
        if dry_run or plan is None:
            return plan, None
        return plan, self.execute(plan, move_context=move_context)
