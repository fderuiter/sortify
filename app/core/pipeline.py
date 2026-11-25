"""The one sort pipeline every front end runs: discover, extract, fit, plan, then move.

Front ends (CLI, daemon, TUI Drop Zone, demo) build an ``AppSession`` and hand it
to ``SortPipeline`` instead of wiring the extractor, analyzer and mover together
themselves. See issue #708 and the system design's "Target architecture".
"""

import contextvars
import inspect
import logging
import os
import threading
from collections.abc import Callable, Collection, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.core.lifecycle import LifecycleTelemetryMixin

logger = logging.getLogger(__name__)

_ACTIVE_PIPELINE: contextvars.ContextVar["SortPipeline | None"] = (
    contextvars.ContextVar("_ACTIVE_PIPELINE", default=None)
)


class PipelineLifecycleState(StrEnum):
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
    detail: str | None = None


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
            issues.append(
                DropPathIssue(raw_path, clean_path, abs_path, "missing", "missing")
            )
            continue
        if scan_filter.is_protected(abs_path):
            issues.append(
                DropPathIssue(raw_path, clean_path, abs_path, "protected", "protected")
            )
            continue
        if ignore_predicate is not None and ignore_predicate(abs_path):
            issues.append(
                DropPathIssue(
                    raw_path, clean_path, abs_path, "ignored", "ignored_predicate"
                )
            )
            continue
        if scan_filter.is_app_bundle(abs_path):
            issues.append(
                DropPathIssue(raw_path, clean_path, abs_path, "ignored", "app_bundle")
            )
            continue
        if scan_filter.is_quarantine(abs_path):
            issues.append(
                DropPathIssue(raw_path, clean_path, abs_path, "ignored", "quarantine")
            )
            continue
        if scan_filter.has_ignored_extension(abs_path):
            issues.append(
                DropPathIssue(
                    raw_path, clean_path, abs_path, "ignored", "ignored_extension"
                )
            )
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


def validate_drop_destination(
    dest_dir: str | None, *, settings: Any = None
) -> tuple[str | None, str | None]:
    """Resolve and validate an optional drop destination directory.

    Parameters
    ----------
    dest_dir : str | None
        Raw destination directory string supplied by CLI or TUI Drop Zone.
    settings : Any, optional
        Application settings containing ``PROTECTED_PATHS``.

    Returns
    -------
    tuple[str | None, str | None]
        ``(resolved_dest, error_reason)`` where ``error_reason`` is
        ``"protected"``, ``"not_directory"``, or ``None``.
    """
    if not dest_dir or not str(dest_dir).strip():
        return None, None
    from app.core.scanner import ScanFilter

    resolved_dest = str(Path(str(dest_dir)).expanduser().resolve())
    if ScanFilter.from_settings(settings=settings).is_protected(resolved_dest):
        return resolved_dest, "protected"
    if os.path.exists(resolved_dest) and not os.path.isdir(resolved_dest):
        return resolved_dest, "not_directory"
    return resolved_dest, None


def expand_drop_scope_files(
    path: str,
    *,
    settings: Any = None,
    group_root: str | None = None,
) -> tuple[str, list[str]]:
    """Resolve the base directory and relative file items for a dropped path.

    Parameters
    ----------
    path : str
        Dropped file or directory path.
    settings : Any, optional
        Application settings used when scanning dropped directories.
    group_root : str | None, optional
        Explicit root folder override for staged files originating from a dropped directory.

    Returns
    -------
    tuple[str, list[str]]
        ``(base_dir, relative_files)`` ready for ``SortPipeline.plan``.
    """
    from app.core.scanner import ScanFilter, get_files_recursively

    scope_dir, scope_files = drop_scope(path)
    if scope_files is None:
        drop_filter = ScanFilter.from_settings(settings=settings, base_dir=scope_dir)
        drop_filter.max_depth = None
        files = get_files_recursively(
            scope_dir, settings=settings, scan_filter=drop_filter
        )
        return scope_dir, files
    effective_root = group_root or scope_dir
    return effective_root, _relativize_items_to_base(scope_files, effective_root)


def group_drop_targets(
    paths: Sequence[str],
    *,
    settings: Any = None,
    staged_roots: dict[str, str | None] | None = None,
) -> dict[str, list[str]]:
    """Group dropped file and folder paths by root directory for batch planning.

    Parameters
    ----------
    paths : Sequence[str]
        Validated dropped file or folder paths.
    settings : Any, optional
        Application settings used when scanning dropped directories.
    staged_roots : dict[str, str | None] | None, optional
        Optional mapping from file path to its staged folder root.

    Returns
    -------
    dict[str, list[str]]
        Mapping of ``base_dir`` to normalized relative file paths.
    """
    groups: dict[str, list[str]] = {}
    root_lookup = staged_roots or {}
    for path in paths:
        group_root, rel_files = expand_drop_scope_files(
            path,
            settings=settings,
            group_root=root_lookup.get(path),
        )
        groups.setdefault(group_root, []).extend(rel_files)
    return groups


def is_rerooted_destination(base_dir: str, resolved_dest: str | None) -> bool:
    """Return True when ``resolved_dest`` points to a folder different from ``base_dir``."""
    if not resolved_dest:
        return False
    return os.path.normcase(os.path.abspath(resolved_dest)) != os.path.normcase(
        os.path.abspath(base_dir)
    )


def apply_drop_reroot(
    session: Any,
    plan: Any,
    base_dir: str,
    resolved_dest: str | None,
    *,
    force_reroot: bool = False,
) -> tuple[Any, bool]:
    """Re-root ``plan`` and update ``session.base_dir`` for a custom drop destination.

    Parameters
    ----------
    session : Any
        Active ``AppSession`` whose ``base_dir`` should move to ``resolved_dest``.
    plan : Any
        Generated sorting plan to re-root.
    base_dir : str
        Original source root directory.
    resolved_dest : str | None
        Validated destination directory, if specified.
    force_reroot : bool, optional
        Re-key ``plan`` even when ``resolved_dest`` equals ``base_dir``.

    Returns
    -------
    tuple[Any, bool]
        ``(updated_plan, re_rooted)`` where ``re_rooted`` indicates ``resolved_dest``
        differed from ``base_dir``.
    """
    re_rooted = is_rerooted_destination(base_dir, resolved_dest)
    if resolved_dest is not None and (re_rooted or force_reroot):
        plan = reroot_plan(plan, resolved_dest, source_dir=base_dir)
        session.base_dir = resolved_dest
    return plan, re_rooted


def restore_rerooted_source_dir(
    source_dir: str | None,
    *,
    was_dir: bool,
    re_rooted: bool,
) -> None:
    """Recreate an emptied source root directory after a re-rooted drop execution."""
    if re_rooted and was_dir and source_dir and not os.path.exists(source_dir):
        os.makedirs(source_dir, exist_ok=True)


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
    staged: list[str] = field(default_factory=list)
    trashed: list[str] = field(default_factory=list)
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
        from app.core.mover import DUPLICATE_STAGING_DIR

        report = summary.get("audit_report") or {}
        for rec in report.get("records") or []:
            src = rec.get("relative_source") or rec.get("source_path") or ""
            dst = (rec.get("relative_destination") or "").replace("\\", "/")
            dst_parts = [p for p in dst.split("/") if p]
            status = rec.get("status")
            if status in ("MISMATCH_FAIL", "MOVE_FAIL"):
                result.failed.append((src, rec.get("error") or status))
            elif status == "SKIPPED":
                result.skipped.append(src)
            elif DUPLICATE_STAGING_DIR in dst_parts:
                result.staged.append(src)
            elif quarantine_dir in dst_parts:
                result.quarantined.append(src)
            else:
                result.moved.append(src)
        if summary.get("trash_handoff"):
            _apply_trash_report_to_result(result, summary["trash_handoff"])
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
        if self.staged:
            parts.append(f"{len(self.staged)} staged")
        if self.trashed:
            parts.append(f"{len(self.trashed)} trashed")
        if self.quarantined:
            parts.append(f"{len(self.quarantined)} quarantined")
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        line = ", ".join(parts) + "."
        return f"Cancelled: {line}" if self.cancelled else line


def _apply_trash_report_to_result(
    result: SortResult, trash_report: dict[str, Any]
) -> None:
    """Reclassify ``result.staged`` entries exported by ``flush_trash`` into ``result.trashed``."""
    exported_raw = trash_report.get("exported") or []
    if not exported_raw:
        return
    exported_norm = {
        os.path.normcase(os.path.normpath(os.path.abspath(str(p))))
        for p in exported_raw
        if p
    }
    report_records = (result.summary.get("audit_report") or {}).get("records") or []
    exported_srcs: list[str] = []
    for rec in report_records:
        dst_abs = rec.get("destination_path") or ""
        if (
            dst_abs
            and os.path.normcase(os.path.normpath(os.path.abspath(str(dst_abs))))
            in exported_norm
        ):
            src = rec.get("relative_source") or rec.get("source_path") or ""
            if src:
                exported_srcs.append(src)
    if exported_srcs:
        remaining_staged = list(result.staged)
        for src in exported_srcs:
            if src in remaining_staged:
                remaining_staged.remove(src)
            if src not in result.trashed:
                result.trashed.append(src)
        result.staged = remaining_staged
    else:
        for p in exported_raw:
            base_name = os.path.basename(str(p))
            if base_name not in result.trashed:
                result.trashed.append(base_name)
        if len(result.staged) >= len(exported_raw):
            result.staged = result.staged[: len(result.staged) - len(exported_raw)]


class SortPipeline(LifecycleTelemetryMixin):
    """Runs a sort over one ``AppSession``.

    The caller owns the session and closes it; the pipeline only drives it.
    """

    def __init__(
        self,
        session: Any = None,
        *,
        base_dir: str | None = None,
        settings: Any = None,
    ):
        super().__init__()
        self.session = session
        self._base_dir = base_dir
        self._settings = settings
        if hasattr(session, "pipeline"):
            session.pipeline = self
        sess_state = getattr(session, "lifecycle_state", None)
        if hasattr(session, "lifecycle_state") and not isinstance(
            sess_state, PipelineLifecycleState
        ):
            session.lifecycle_state = PipelineLifecycleState.CREATED
            sess_state = PipelineLifecycleState.CREATED
        self._state = (
            PipelineLifecycleState.PLANNED
            if sess_state == PipelineLifecycleState.PLANNED
            else PipelineLifecycleState.CREATED
        )
        self._lock = threading.RLock()
        self._rollback_completed: bool | None = None
        self._planned_items: list[Any] = []
        self._defer_unarchive_children: bool = False
        self._unarchive_depth: int = 0

    @property
    def planned_items(self) -> list[Any]:
        """Return a copy of the items discovered or filtered during the latest plan."""
        return list(self._planned_items)

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

        active_token = _ACTIVE_PIPELINE.set(self)
        try:
            from app.core.scanner import (
                ScanFilter,
                discover_existing_folders,
                get_files_from_sources,
                get_files_recursively,
                parse_item_identifier,
            )

            raw_source_dirs = getattr(self.session, "source_dirs", None)
            candidate_roots: list[str] = []
            if isinstance(raw_source_dirs, (list, tuple, set)):
                candidate_roots = [
                    str(d)
                    for d in raw_source_dirs
                    if isinstance(d, (str, os.PathLike)) and str(d)
                ]
            if not candidate_roots and self.base_dir:
                candidate_roots = [self.base_dir]

            resolved_source_dirs: list[str] = []
            for root_dir in candidate_roots:
                parent_dir = os.path.dirname(os.path.abspath(root_dir))
                root_parent_filter = ScanFilter.from_settings(
                    settings=self.settings, base_dir=parent_dir
                )
                if not root_parent_filter.is_quarantine(root_dir):
                    resolved_source_dirs.append(root_dir)

            scan_filter = ScanFilter.from_settings(
                settings=self.settings, base_dir=self.base_dir
            )
            explicit_items = items is not None
            if items is None:
                if len(resolved_source_dirs) > 1:
                    items = get_files_from_sources(
                        resolved_source_dirs,
                        settings=self.settings,
                    )
                elif len(resolved_source_dirs) == 1:
                    single_root = resolved_source_dirs[0]
                    single_filter = ScanFilter.from_settings(
                        settings=self.settings, base_dir=single_root
                    )
                    items = get_files_recursively(
                        single_root,
                        settings=self.settings,
                        scan_filter=single_filter,
                    )
                else:
                    items = []
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

            self._planned_items = list(items)

            from app.core.rule_scopes import resolve_scoped_settings

            effective_plan_root = (
                resolved_source_dirs[0]
                if len(resolved_source_dirs) == 1
                else self.base_dir
            )
            plan_settings = (
                resolve_scoped_settings(
                    self.settings, effective_plan_root, fallback_to_global=True
                )
                if effective_plan_root
                else self.settings
            )

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
                        plan_settings,
                        self.session.db,
                        progress_callback=progress_callback,
                        cancel_check=cancel_check,
                        source_dirs=resolved_source_dirs or None,
                    )
                    or []
                )
                if cancel_check and cancel_check():
                    self.transition_to(PipelineLifecycleState.CANCELLED)
                    self.emit_telemetry("plan_cancelled")
                    return None

            if not bool(fast_path_only):
                from app.core.extractor import build_corpus_generator
                from app.core.progress import emit_progress

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
                        if isinstance(raw_workers, int)
                        and not isinstance(raw_workers, bool)
                        and raw_workers > 0
                        else 2
                    )
                    total_extract = len(items_to_extract)
                    done_extract = 0
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
                        done_extract += len(chunk)
                        if progress_callback is not None and total_extract > 0:
                            emit_progress(
                                progress_callback,
                                done_extract / total_extract,
                                stage=f"Extracting text ({done_extract}/{total_extract})",
                                unit_count=done_extract,
                                unit_type="files",
                            )

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
            effective_target_paths = (
                list(items)
                if (target_paths is None and explicit_items)
                else target_paths
            )
            try:
                gen_sig = inspect.signature(self.session.generate_sorting_plan)
                has_var_kw = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD
                    for p in gen_sig.parameters.values()
                )
                if (
                    target_paths is None
                    and effective_target_paths is not None
                    and (has_var_kw or "target_paths" in gen_sig.parameters)
                ):
                    plan_kwargs["target_paths"] = effective_target_paths
                if resolved_existing and (
                    has_var_kw or "existing_folders" in gen_sig.parameters
                ):
                    plan_kwargs["existing_folders"] = resolved_existing
                if dest_dir and (has_var_kw or "dest_dir" in gen_sig.parameters):
                    plan_kwargs["dest_dir"] = dest_dir
            except (ValueError, TypeError):
                if target_paths is None and effective_target_paths is not None:
                    plan_kwargs["target_paths"] = effective_target_paths
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
        finally:
            _ACTIVE_PIPELINE.reset(active_token)

    def _sort_unarchived_children(
        self,
        unarchived_children: list[str],
        parent_result: SortResult,
        *,
        cancel_check: Callable | None = None,
    ) -> None:
        """Plan and execute sorting for extracted child files under the parent session."""
        if not unarchived_children or not self.base_dir or not parent_result.session_id:
            return
        if cancel_check and cancel_check():
            return

        from app.core.forensic_scanner import ForensicScanner
        from app.core.path_utils import safe_relpath

        depth = int(getattr(self, "_unarchive_depth", 0))
        if depth >= 3:
            logger.warning(
                "Skipping recursive unarchive child sorting beyond depth limit (%d).",
                depth,
            )
            return

        scanner = ForensicScanner()
        existing_children = [p for p in unarchived_children if p and os.path.isfile(p)]
        if not existing_children:
            return
        if len(existing_children) > scanner.max_files:
            logger.warning(
                "Skipping child sorting: extracted child count %d exceeds limit %d.",
                len(existing_children),
                scanner.max_files,
            )
            return
        total_child_bytes = sum(
            os.path.getsize(p) for p in existing_children if os.path.exists(p)
        )
        if total_child_bytes > scanner.max_total_size_bytes:
            logger.warning(
                "Skipping child sorting: extracted child bytes %d exceed limit %d.",
                total_child_bytes,
                scanner.max_total_size_bytes,
            )
            return

        rel_children: list[str] = []
        for child_abs in existing_children:
            try:
                rel_p = safe_relpath(child_abs, self.base_dir).replace("\\", "/")
            except ValueError:
                continue
            if rel_p and not rel_p.startswith(".."):
                rel_children.append(rel_p)
        if not rel_children:
            return

        child_pipeline = SortPipeline(
            self.session, base_dir=self.base_dir, settings=self.settings
        )
        child_pipeline._unarchive_depth = depth + 1
        child_plan = child_pipeline.plan(
            rel_children,
            target_paths=rel_children,
            cancel_check=cancel_check,
        )
        if not child_plan:
            return

        child_result = child_pipeline.execute(
            child_plan,
            parent_session_id=parent_result.session_id,
            cancel_check=cancel_check,
        )
        parent_result.moved.extend(child_result.moved)
        parent_result.staged.extend(child_result.staged)
        parent_result.trashed.extend(child_result.trashed)
        parent_result.quarantined.extend(child_result.quarantined)
        parent_result.skipped.extend(child_result.skipped)
        parent_result.failed.extend(child_result.failed)
        parent_records = parent_result.summary.setdefault(
            "audit_report", {}
        ).setdefault("records", [])
        child_records = (
            (child_result.summary or {}).get("audit_report", {}).get("records", [])
        )
        if child_records:
            parent_records.extend(child_records)

    def execute(
        self,
        plan,
        *,
        move_context: Callable[[Any], AbstractContextManager[Any]] | None = None,
        flush_trash: bool = False,
        flush_trashed_only: bool = False,
        trash_fn: Callable[[str], Any] | None = None,
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
            side_effect = getattr(self.session.execute_moves, "side_effect", None)
            fn = (
                side_effect
                if (
                    callable(side_effect)
                    and not (
                        isinstance(side_effect, type)
                        and issubclass(side_effect, BaseException)
                    )
                )
                else self.session.execute_moves
            )
            sig = inspect.signature(fn)
            has_var_kw = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
            if not has_var_kw:
                call_kwargs = {
                    k: v for k, v in call_kwargs.items() if k in sig.parameters
                }
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
            unarchived_children = list((summary or {}).get("unarchived_children") or [])
            if (
                unarchived_children
                and not getattr(self, "_defer_unarchive_children", False)
                and not result.cancelled
                and not summary.get("cancelled")
                and not result.failed
            ):
                self._sort_unarchived_children(
                    unarchived_children,
                    result,
                    cancel_check=call_kwargs.get("cancel_check"),
                )

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
                if flush_trash and (result.session_id or not flush_trashed_only):
                    trash_report = self.flush_trash(
                        result.session_id or None,
                        trashed_only=flush_trashed_only,
                        trash_fn=trash_fn,
                    )
                    result.summary["trash_handoff"] = trash_report
                    _apply_trash_report_to_result(result, trash_report)

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

        from app.core.mover import is_subpath_or_equal, overwrite_backup_dir
        from app.core.moves import EXPORTED_TO_TRASH, FAILED, INTENT
        from app.core.path_utils import safe_relpath

        lease = None
        try:
            if (
                self.session is not None
                and getattr(self.session, "history_manager", None) is not None
            ):
                history = self.session.history_manager
            else:
                from app.core.services import acquire_services

                lease = acquire_services()
                history = lease.history

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
                self.emit_telemetry(
                    "undo_error",
                    session_id=session_id,
                    error=f"Session not found: {session_id}",
                )
                raise ValueError(f"Session not found: {session_id}")
            session_backup_dir = overwrite_backup_dir(base_dir, session_id)
            ephemeral_paths: set[str] = set()
            moves = []
            trashed_moves = []
            for m in sorted(
                history.moves.list_moves(session_id),
                key=lambda item: item.seq,
            ):
                if m.state in (INTENT, FAILED):
                    continue
                if m.state == EXPORTED_TO_TRASH:
                    trashed_moves.append(m)
                    continue
                norm_src = os.path.normcase(os.path.abspath(m.src))
                norm_dst = os.path.normcase(os.path.abspath(m.dst))
                if is_subpath_or_equal(m.src, session_backup_dir):
                    ephemeral_paths.add(norm_dst)
                    continue
                if norm_src in ephemeral_paths:
                    ephemeral_paths.discard(norm_src)
                    ephemeral_paths.add(norm_dst)
                    continue
                moves.append(m)
            if selected_step_ids is not None:
                sel_set = {str(x) for x in selected_step_ids}
                moves = [m for m in moves if str(m.seq) in sel_set]
                trashed_moves = [m for m in trashed_moves if str(m.seq) in sel_set]

            result = SortResult(
                session_id=session_id, state=PipelineLifecycleState.ROLLING_BACK
            )
            if trashed_moves:
                for tm in trashed_moves:
                    result.trashed.append(
                        safe_relpath(tm.src, base_dir).replace("\\", "/")
                    )
                result.summary["trashed_unrestorable"] = len(trashed_moves)
                result.summary["trashed_paths"] = list(result.trashed)
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
        except Exception as err:
            if self.state != PipelineLifecycleState.FAILED:
                try:
                    self.transition_to(PipelineLifecycleState.FAILED)
                except IllegalStateTransitionError as trans_err:
                    logger.debug(f"Failed to transition to FAILED state: {trans_err}")
                self.emit_telemetry("undo_error", session_id=session_id, error=str(err))
            raise
        finally:
            if lease is not None:
                lease.close()

    def flush_trash(
        self,
        session_id: str | None = None,
        *,
        max_age_days: float | None = None,
        trashed_only: bool = False,
        trash_fn: Callable[[str], Any] | None = None,
    ) -> dict[str, Any]:
        """Export staged duplicates to native OS Trash and record irreversible handoff."""
        history = getattr(self.session, "history_manager", None)
        if history is None or not hasattr(history, "flush_staged_duplicates"):
            return {"exported": [], "kept_staged": [], "errors": {}}
        return history.flush_staged_duplicates(
            session_id=session_id,
            base_dir=self.base_dir or None,
            max_age_days=max_age_days,
            trashed_only=trashed_only,
            trash_fn=trash_fn,
        )

    def run(
        self,
        items: list | None = None,
        *,
        dry_run: bool = False,
        move_context: Callable[[Any], AbstractContextManager[Any]] | None = None,
        flush_trash: bool = False,
        trash_fn: Callable[[str], Any] | None = None,
        **kwargs,
    ):
        """Plan, then execute unless ``dry_run``. Returns ``(plan, result)``.

        A dry run's result is ``None``.
        """
        plan = self.plan(items, **kwargs)
        if dry_run or plan is None:
            return plan, None
        return plan, self.execute(
            plan,
            move_context=move_context,
            flush_trash=flush_trash,
            trash_fn=trash_fn,
        )


@dataclass
class DropBatchPlanItem:
    """Preview and override state for one file in a planned drop batch."""

    item_id: str
    queue_priority: int
    source_root: str
    source_path: str
    dest_base_dir: str
    target_folder: str
    target_filename: str
    dest_path: str
    conflict_policy: str = "rename"
    is_excluded: bool = False
    has_collision: bool = False
    node_ref: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class DropBatchGroupPlan:
    """Planned drop unit for one source root directory."""

    source_root: str
    rel_files: list[str]
    target_paths: list[str]
    session: Any
    pipeline: SortPipeline
    plan: dict[str, Any]
    re_rooted: bool = False
    was_dir: bool = False


@dataclass
class DropBatchPlan:
    """Aggregated drop plan, verification warnings, and item previews across all roots."""

    validated_paths: list[str]
    issues: list[DropPathIssue]
    resolved_dest: str | None
    groups: list[DropBatchGroupPlan]
    unified_plan: dict[str, Any] | None = None
    conflict_policy: str = "rename"
    warnings: list[str] = field(default_factory=list)
    collisions: list[dict[str, Any]] = field(default_factory=list)
    collision_groups: list[dict[str, Any]] = field(default_factory=list)
    items: list[DropBatchPlanItem] = field(default_factory=list)
    plan_lines: list[str] = field(default_factory=list)
    closed: bool = False


@dataclass
class DropBatchResult:
    """Outcome of executing (or dry-running) a ``DropBatchPlan``."""

    session_id: str | None
    resolved_dest: str | None
    moved_count: int = 0
    staged_count: int = 0
    trashed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    cancelled: bool = False
    dry_run: bool = False
    target_results: list[dict[str, Any]] = field(default_factory=list)
    audit_records: list[dict[str, Any]] = field(default_factory=list)
    trash_handoff: dict[str, Any] | None = None


def _unwrap_plan_dict(plan_obj: Any) -> dict[str, Any]:
    if hasattr(plan_obj, "plan") and isinstance(plan_obj.plan, dict):
        return dict(plan_obj.plan)
    if isinstance(plan_obj, dict):
        return dict(plan_obj)
    return {}


def _is_file_leaf_node(content: Any) -> bool:
    from app.core.domain_contracts import _get_val

    node_type = _get_val(content, "node_type") or _get_val(content, "__type__")
    if content is None or node_type == "file":
        return True
    if hasattr(content, "model_dump") and node_type != "directory":
        return True
    if isinstance(content, dict) and node_type != "directory":
        return (
            "relative_source" in content
            or "target_filename" in content
            or "routed_by" in content
            or "source_path" in content
        )
    return False


def _annotate_drop_plan_nodes(
    node: Any,
    *,
    source_root: str,
    queue_priority_by_abs: dict[str, int],
    conflict_policy: str | None = None,
    fallback_counter: list[int] | None = None,
) -> dict[str, Any]:
    """Attach ``queue_priority``, ``source_root``, ``source_path``, and ``target_filename`` to leaf file dicts."""
    from app.core.scanner import parse_item_identifier

    if not isinstance(node, dict):
        return {}
    counter = fallback_counter if fallback_counter is not None else [100000]
    annotated: dict[str, Any] = {}
    for key, content in node.items():
        if str(key).startswith("__"):
            annotated[key] = content
            continue
        if _is_file_leaf_node(content):
            copied: dict[str, Any] = (
                content.model_dump(by_alias=True)
                if hasattr(content, "model_dump") and not isinstance(content, dict)
                else (dict(content) if isinstance(content, dict) else {})
            )
            copied.setdefault("__type__", "file")
            copied.setdefault("source_root", source_root)
            _, rel_key = parse_item_identifier(str(key), source_root)
            raw_sp = copied.get("source_path")
            if raw_sp and os.path.isabs(str(raw_sp)):
                abs_sp = os.path.normpath(str(raw_sp))
            else:
                rel_sp = str(copied.get("relative_source") or rel_key)
                _, clean_rel = parse_item_identifier(rel_sp, source_root)
                abs_sp = os.path.normpath(os.path.join(source_root, clean_rel))
            copied["source_path"] = abs_sp
            if not copied.get("relative_source"):
                try:
                    copied["relative_source"] = os.path.relpath(
                        abs_sp, source_root
                    ).replace("\\", "/")
                except ValueError:
                    copied["relative_source"] = rel_key
            if not copied.get("target_filename"):
                copied["target_filename"] = os.path.basename(rel_key)
            norm_abs = os.path.normcase(os.path.abspath(abs_sp))
            qp = queue_priority_by_abs.get(norm_abs)
            if qp is None:
                qp = counter[0]
                counter[0] += 1
            copied["queue_priority"] = int(qp)
            if conflict_policy in ("rename", "overwrite", "skip"):
                copied["conflict_policy"] = conflict_policy
                copied["overwrite"] = conflict_policy == "overwrite"
            annotated[key] = copied
        elif isinstance(content, dict):
            annotated[key] = _annotate_drop_plan_nodes(
                content,
                source_root=source_root,
                queue_priority_by_abs=queue_priority_by_abs,
                conflict_policy=conflict_policy,
                fallback_counter=counter,
            )
        else:
            annotated[key] = content
    return annotated


def _merge_rerooted_plans_into_dest(
    groups: Sequence[DropBatchGroupPlan],
) -> dict[str, Any]:
    """Merge re-rooted group plans into a single plan using namespaced ``root::rel`` leaf keys."""
    from app.core.scanner import parse_item_identifier

    merged: dict[str, Any] = {}

    def _merge_into(
        target_dict: dict[str, Any], src_dict: dict[str, Any], source_root: str
    ) -> None:
        norm_root = os.path.normpath(source_root) if source_root else ""
        for key, content in src_dict.items():
            if str(key).startswith("__"):
                target_dict.setdefault(key, content)
                continue
            if _is_file_leaf_node(content) and isinstance(content, dict):
                _, rel_key = parse_item_identifier(str(key), source_root)
                rel_src = str(content.get("relative_source") or rel_key)
                _, clean_rel = parse_item_identifier(rel_src, source_root)
                if not content.get("target_filename"):
                    content["target_filename"] = os.path.basename(rel_key)
                ns_key = f"{norm_root}::{clean_rel}" if norm_root else clean_rel
                if ns_key in target_dict:
                    suffix = 1
                    while f"{ns_key}#{suffix}" in target_dict:
                        suffix += 1
                    ns_key = f"{ns_key}#{suffix}"
                target_dict[ns_key] = content
            elif isinstance(content, dict):
                bucket = target_dict.get(key)
                if not isinstance(bucket, dict) or _is_file_leaf_node(bucket):
                    bucket = {}
                    target_dict[key] = bucket
                _merge_into(bucket, content, source_root)
            else:
                target_dict[key] = content

    for grp in groups:
        _merge_into(merged, grp.plan, grp.source_root)
    return merged


def _collect_all_leaf_nodes(
    node: Any,
    dest_base_dir: str,
    current_dest: str = "",
) -> list[tuple[str, dict[str, Any], str, str]]:
    """Collect ``(key, leaf_dict, dest_base_dir, current_dest)`` across all leaf nodes, including excluded ones."""
    if not isinstance(node, dict):
        return []
    found: list[tuple[str, dict[str, Any], str, str]] = []
    for key, content in node.items():
        if str(key).startswith("__"):
            continue
        if _is_file_leaf_node(content) and isinstance(content, dict):
            found.append((str(key), content, dest_base_dir, current_dest))
        elif isinstance(content, dict):
            next_dest = (
                str(key)
                if (not current_dest and os.path.isabs(str(key)))
                else os.path.join(current_dest, str(key))
            )
            found.extend(_collect_all_leaf_nodes(content, dest_base_dir, next_dest))
    return found


class DropBatchCoordinator:
    """Single orchestration coordinator for CLI and TUI drop triage batches."""

    def __init__(
        self,
        settings: Any = None,
        *,
        session_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings
        self._session_factory = session_factory

    def _create_session(self, base_dir: str) -> Any:
        if self._session_factory is not None:
            return self._session_factory(self.settings, base_dir=base_dir)
        from app.core.session import AppSession

        return AppSession(self.settings, base_dir=base_dir)

    def validate(
        self,
        paths: Sequence[Any],
        *,
        dest_dir: str | None = None,
        ignore_predicate: Callable[[str], bool] | None = None,
    ) -> tuple[list[str], list[DropPathIssue], str | None, str | None]:
        """Validate dropped paths and optional destination directory."""
        validated_paths, issues = validate_drop_paths(
            paths,
            settings=self.settings,
            ignore_predicate=ignore_predicate,
        )
        resolved_dest, dest_err = validate_drop_destination(
            dest_dir, settings=self.settings
        )
        return validated_paths, issues, resolved_dest, dest_err

    def plan_batch(
        self,
        paths: Sequence[str],
        *,
        dest_dir: str | None = None,
        staged_roots: dict[str, str | None] | None = None,
        conflict_policy: str | None = None,
        progress_callback: Callable[[Any], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> DropBatchPlan:
        """Expand, scope-plan, re-root, and pre-verify a batch of dropped paths."""
        validated_paths, issues, resolved_dest, dest_err = self.validate(
            paths, dest_dir=dest_dir
        )
        if dest_err == "protected":
            raise PermissionError(f"Destination path '{dest_dir}' is protected.")
        if dest_err == "not_directory":
            raise NotADirectoryError(
                f"Destination path '{dest_dir}' is not a directory."
            )

        default_policy = str(
            conflict_policy
            or getattr(self.settings, "CONFLICT_POLICY", None)
            or "rename"
        ).lower()
        if default_policy not in ("rename", "overwrite", "skip"):
            default_policy = "rename"

        root_lookup = staged_roots or {}
        queue_priority_by_abs: dict[str, int] = {}
        group_files: dict[str, list[str]] = {}
        group_targets: dict[str, list[str]] = {}
        group_was_dir: dict[str, bool] = {}
        seen_rel_by_root: dict[str, set[str]] = {}
        next_qp = 0

        for raw_target in validated_paths:
            explicit_root = root_lookup.get(raw_target)
            group_root, rel_files = expand_drop_scope_files(
                raw_target,
                settings=self.settings,
                group_root=explicit_root,
            )
            norm_root = os.path.normpath(os.path.abspath(group_root))
            is_dir_drop = os.path.isdir(raw_target) or (explicit_root is not None)
            if is_dir_drop:
                group_was_dir[norm_root] = True
            else:
                group_was_dir.setdefault(norm_root, False)
            group_targets.setdefault(norm_root, []).append(raw_target)
            bucket = group_files.setdefault(norm_root, [])
            seen_rels = seen_rel_by_root.setdefault(norm_root, set())

            for rel_f in rel_files:
                abs_f = os.path.normpath(os.path.join(norm_root, rel_f))
                norm_abs = os.path.normcase(os.path.abspath(abs_f))
                if norm_abs not in queue_priority_by_abs:
                    queue_priority_by_abs[norm_abs] = next_qp
                    next_qp += 1
                norm_rel_key = os.path.normcase(rel_f.replace("\\", "/"))
                if norm_rel_key not in seen_rels:
                    seen_rels.add(norm_rel_key)
                    bucket.append(rel_f)

        groups: list[DropBatchGroupPlan] = []
        total_groups = max(1, len(group_files))
        try:
            for idx, (group_root, rel_files) in enumerate(group_files.items()):
                if cancel_check and cancel_check():
                    break
                session = self._create_session(group_root)
                pipeline = SortPipeline(session)

                def _group_progress(
                    info: Any, _idx: int = idx, _total: int = total_groups
                ) -> None:
                    if progress_callback is None:
                        return
                    if isinstance(info, dict):
                        scaled = dict(info)
                        raw_p = float(info.get("progress", 0.0) or 0.0)
                        scaled["progress"] = (_idx + raw_p) / _total
                        progress_callback(scaled)
                    else:
                        progress_callback(info)

                raw_plan = pipeline.plan(
                    rel_files,
                    progress_callback=_group_progress
                    if progress_callback is not None
                    else None,
                    cancel_check=cancel_check,
                    dest_dir=resolved_dest,
                )
                plan_dict = _unwrap_plan_dict(raw_plan)
                bound_plan = _bind_plan_sources(plan_dict, group_root)
                annotated_plan = _annotate_drop_plan_nodes(
                    bound_plan,
                    source_root=group_root,
                    queue_priority_by_abs=queue_priority_by_abs,
                    conflict_policy=conflict_policy,
                )
                final_plan, re_rooted = apply_drop_reroot(
                    session,
                    annotated_plan,
                    group_root,
                    resolved_dest,
                    force_reroot=resolved_dest is not None,
                )
                groups.append(
                    DropBatchGroupPlan(
                        source_root=group_root,
                        rel_files=rel_files,
                        target_paths=group_targets.get(group_root, []),
                        session=session,
                        pipeline=pipeline,
                        plan=_unwrap_plan_dict(final_plan),
                        re_rooted=re_rooted,
                        was_dir=group_was_dir.get(group_root, False),
                    )
                )
        except Exception:
            for grp in groups:
                try:
                    grp.session.close()
                except Exception as close_err:  # noqa: BLE001
                    logger.debug(
                        "Failed to close session after batch plan error: %s",
                        close_err,
                    )
            raise

        batch_plan = DropBatchPlan(
            validated_paths=validated_paths,
            issues=issues,
            resolved_dest=resolved_dest,
            groups=groups,
            conflict_policy=default_policy,
        )
        self.refresh_verification(batch_plan)
        return batch_plan

    def refresh_verification(self, batch_plan: DropBatchPlan) -> DropBatchPlan:
        """Run ``VerificationEngine.verify_plan_integrity`` and rebuild preview items in queue order."""
        from app.core.domain_contracts import _get_val
        from app.core.path_utils import safe_relpath
        from app.core.verifier import VerificationEngine

        all_warnings: list[str] = []
        all_collisions: list[dict[str, Any]] = []
        all_collision_groups: list[dict[str, Any]] = []
        colliding_sources: set[str] = set()
        colliding_dests: set[str] = set()

        if batch_plan.resolved_dest and batch_plan.groups:
            os.makedirs(batch_plan.resolved_dest, exist_ok=True)
            unified = (
                batch_plan.unified_plan
                if batch_plan.unified_plan is not None
                else _merge_rerooted_plans_into_dest(batch_plan.groups)
            )
            vres = VerificationEngine.verify_plan_integrity(
                batch_plan.resolved_dest,
                unified,
                conflict_policy=batch_plan.conflict_policy,
            )
            batch_plan.unified_plan = unified
            all_warnings.extend(vres.get("warnings") or [])
            all_collisions.extend(vres.get("collisions") or [])
            cgroups = VerificationEngine.get_collision_groups(
                batch_plan.resolved_dest, unified, include_resolved=True
            )
            all_collision_groups.extend(cgroups)
            leaf_records = _collect_all_leaf_nodes(unified, batch_plan.resolved_dest)
        else:
            batch_plan.unified_plan = None
            leaf_records = []
            for grp in batch_plan.groups:
                vres = VerificationEngine.verify_plan_integrity(
                    grp.source_root,
                    grp.plan,
                    conflict_policy=batch_plan.conflict_policy,
                )
                all_warnings.extend(vres.get("warnings") or [])
                all_collisions.extend(vres.get("collisions") or [])
                cgroups = VerificationEngine.get_collision_groups(
                    grp.source_root, grp.plan, include_resolved=True
                )
                all_collision_groups.extend(cgroups)
                leaf_records.extend(_collect_all_leaf_nodes(grp.plan, grp.source_root))

        for col in all_collisions:
            src_c = col.get("source")
            tgt_c = col.get("target")
            if src_c:
                colliding_sources.add(os.path.normcase(os.path.abspath(str(src_c))))
            if tgt_c:
                colliding_dests.add(os.path.normcase(os.path.abspath(str(tgt_c))))
        for cg in all_collision_groups:
            tgt_p = cg.get("target_path")
            if tgt_p:
                colliding_dests.add(os.path.normcase(os.path.abspath(str(tgt_p))))
            for it in cg.get("items") or []:
                sp = it.get("source_path")
                if sp:
                    colliding_sources.add(os.path.normcase(os.path.abspath(str(sp))))

        for cg in all_collision_groups:
            tgt_p = cg.get("target_path") or cg.get("target_filename") or ""
            n_items = len(cg.get("items") or [])
            if cg.get("existing_file_on_disk"):
                msg = f"Destination collision: '{tgt_p}' already exists on disk."
            else:
                msg = f"Destination collision: {n_items} items target '{tgt_p}'."
            if msg not in all_warnings:
                all_warnings.append(msg)

        preview_items: list[DropBatchPlanItem] = []
        for idx, (key, leaf_dict, dest_base_dir, current_dest) in enumerate(
            leaf_records
        ):
            src_root = str(leaf_dict.get("source_root") or dest_base_dir)
            src_path = str(
                leaf_dict.get("source_path") or os.path.join(src_root, str(key))
            )
            abs_src = os.path.normpath(os.path.abspath(src_path))
            item_id = os.path.normcase(abs_src)
            raw_qp = _get_val(leaf_dict, "queue_priority")
            try:
                qp = int(raw_qp) if raw_qp is not None else idx
            except (TypeError, ValueError):
                qp = idx
            tgt_fn = str(leaf_dict.get("target_filename") or os.path.basename(abs_src))
            dest_override = leaf_dict.get("destination_override")
            eff_folder_raw = (
                str(dest_override) if dest_override else str(current_dest or "")
            )
            if eff_folder_raw and os.path.isabs(eff_folder_raw):
                abs_dest_folder = os.path.normpath(eff_folder_raw)
            elif eff_folder_raw:
                abs_dest_folder = os.path.normpath(
                    os.path.join(dest_base_dir, eff_folder_raw)
                )
            else:
                abs_dest_folder = os.path.normpath(dest_base_dir)
            dest_path = os.path.normpath(os.path.join(abs_dest_folder, tgt_fn))
            rel_folder = safe_relpath(abs_dest_folder, dest_base_dir).replace("\\", "/")
            if not rel_folder:
                rel_folder = "."
            node_pol = str(
                leaf_dict.get("conflict_policy") or batch_plan.conflict_policy
            ).lower()
            if bool(leaf_dict.get("overwrite") or leaf_dict.get("allow_overwrite")):
                node_pol = "overwrite"
            is_excl = bool(
                leaf_dict.get("is_excluded")
                or leaf_dict.get("excluded")
                or leaf_dict.get("is_checked") is False
            )
            norm_dest = os.path.normcase(os.path.abspath(dest_path))
            has_col = item_id in colliding_sources or norm_dest in colliding_dests
            preview_items.append(
                DropBatchPlanItem(
                    item_id=item_id,
                    queue_priority=qp,
                    source_root=src_root,
                    source_path=abs_src,
                    dest_base_dir=dest_base_dir,
                    target_folder=rel_folder,
                    target_filename=tgt_fn,
                    dest_path=dest_path,
                    conflict_policy=node_pol,
                    is_excluded=is_excl,
                    has_collision=has_col,
                    node_ref=leaf_dict,
                )
            )

        preview_items.sort(key=lambda it: (it.queue_priority, it.source_path))
        plan_lines: list[str] = []
        for it in preview_items:
            dup_act = (
                str(it.node_ref.get("duplicate_action") or "").strip().lower()
                if isinstance(it.node_ref, dict)
                else ""
            )
            pol_act = (
                str(it.node_ref.get("policy_action") or "").strip().lower()
                if isinstance(it.node_ref, dict)
                else ""
            )
            if it.is_excluded:
                if dup_act == "skip" or (
                    isinstance(it.node_ref, dict) and it.node_ref.get("is_duplicate")
                ):
                    plan_lines.append(
                        f"[DUPLICATE: skip] {os.path.basename(it.source_path)}"
                    )
                else:
                    plan_lines.append(f"[EXCLUDED] {os.path.basename(it.source_path)}")
                continue
            if pol_act == "trash" or dup_act == "trash":
                plan_lines.append(
                    f"[DUPLICATE: trash] {os.path.basename(it.source_path)}  ->  OS Trash (via verified staging)"
                )
                continue
            if dup_act == "stage":
                plan_lines.append(
                    f"[DUPLICATE: stage] {os.path.basename(it.source_path)}  ->  .sortify_duplicates/"
                )
                continue
            pol_tag = (
                f" [{it.conflict_policy}]"
                if it.has_collision or it.conflict_policy != "rename"
                else ""
            )
            col_tag = " [COLLISION]" if it.has_collision else ""
            plan_lines.append(
                f"{os.path.basename(it.source_path)}  ->  {it.dest_path}{col_tag}{pol_tag}"
            )

        batch_plan.warnings = list(dict.fromkeys(all_warnings))
        batch_plan.collisions = all_collisions
        batch_plan.collision_groups = all_collision_groups
        batch_plan.items = preview_items
        batch_plan.plan_lines = plan_lines
        return batch_plan

    def apply_user_overrides(
        self,
        batch_plan: DropBatchPlan,
        *,
        conflict_policy: str | None = None,
        item_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> DropBatchPlan:
        """Apply conflict policy, folder overrides, and exclusions onto planned nodes and re-verify."""
        from app.core.mover import is_subpath_or_equal
        from app.core.scanner import ScanFilter

        scan_filter = ScanFilter.from_settings(settings=self.settings)

        if conflict_policy is not None:
            pol = str(conflict_policy).strip().lower()
            if pol not in ("rename", "overwrite", "skip"):
                raise ValueError(f"Unsupported conflict policy: {conflict_policy}")
            batch_plan.conflict_policy = pol
            for it in batch_plan.items:
                it.node_ref["conflict_policy"] = pol
                it.node_ref["overwrite"] = pol == "overwrite"

        if item_overrides:
            by_id = {it.item_id: it for it in batch_plan.items}
            by_src = {it.source_path: it for it in batch_plan.items}
            for raw_key, spec in item_overrides.items():
                if not isinstance(spec, dict):
                    continue
                norm_key = os.path.normcase(os.path.abspath(str(raw_key)))
                target_item = by_id.get(norm_key) or by_src.get(str(raw_key))
                if target_item is None:
                    continue
                node = target_item.node_ref
                if "is_excluded" in spec:
                    excl = bool(spec["is_excluded"])
                    node["is_excluded"] = excl
                    node["is_checked"] = not excl
                if "conflict_policy" in spec and spec["conflict_policy"]:
                    item_pol = str(spec["conflict_policy"]).strip().lower()
                    if item_pol not in ("rename", "overwrite", "skip"):
                        raise ValueError(
                            f"Unsupported item conflict policy: {spec['conflict_policy']}"
                        )
                    node["conflict_policy"] = item_pol
                    node["overwrite"] = item_pol == "overwrite"
                if "target_folder" in spec and spec["target_folder"] is not None:
                    raw_folder = str(spec["target_folder"]).strip()
                    if not raw_folder or raw_folder == ".":
                        resolved_folder = os.path.normpath(target_item.dest_base_dir)
                    else:
                        parts = [
                            p
                            for p in raw_folder.replace("\\", "/").split("/")
                            if p and p != "."
                        ]
                        if ".." in parts:
                            raise ValueError(
                                f"Folder override '{raw_folder}' cannot traverse parent directories."
                            )
                        if os.path.isabs(raw_folder):
                            resolved_folder = os.path.normpath(raw_folder)
                        else:
                            resolved_folder = os.path.normpath(
                                os.path.join(target_item.dest_base_dir, *parts)
                            )
                    if not is_subpath_or_equal(
                        resolved_folder, target_item.dest_base_dir
                    ):
                        raise ValueError(
                            f"Folder override '{raw_folder}' escapes base directory '{target_item.dest_base_dir}'."
                        )
                    if scan_filter.is_protected(resolved_folder):
                        raise PermissionError(
                            f"Folder override '{resolved_folder}' is a protected path."
                        )
                    node["destination_override"] = resolved_folder

        return self.refresh_verification(batch_plan)

    def close_batch(self, batch_plan: DropBatchPlan) -> None:
        """Close all ``AppSession`` instances owned by ``batch_plan``."""
        if batch_plan.closed:
            return
        batch_plan.closed = True
        for grp in batch_plan.groups:
            try:
                grp.session.close()
            except Exception as close_err:  # noqa: BLE001
                logger.debug("Failed to close batch session: %s", close_err)

    def execute_batch(
        self,
        batch_plan: DropBatchPlan,
        *,
        dry_run: bool = False,
        flush_trash: bool = False,
        flush_trashed_only: bool = False,
        trash_fn: Callable[[str], Any] | None = None,
        progress_callback: Callable[[Any], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        close_sessions: bool = True,
    ) -> DropBatchResult:
        """Execute ``batch_plan`` under sorted folder locks and a single shared undo ``session_id``."""
        from app.core.domain_contracts import _make_json_serializable
        from app.core.mover import DUPLICATE_STAGING_DIR, execute_moves
        from app.core.moves import DONE, EXPORTED_TO_TRASH, VERIFIED
        from app.core.session import AppSession

        try:
            if not batch_plan.groups:
                return DropBatchResult(
                    session_id=None,
                    resolved_dest=batch_plan.resolved_dest,
                    dry_run=dry_run,
                )

            if dry_run:
                target_results: list[dict[str, Any]] = []
                for grp in batch_plan.groups:
                    serializable_plan = _make_json_serializable(grp.plan)
                    targets = grp.target_paths or [grp.source_root]
                    for tgt in targets:
                        target_results.append(
                            {
                                "target": tgt,
                                "base_dir": grp.source_root,
                                "plan": serializable_plan,
                                "summary": {
                                    "status": "dry_run",
                                    "plan": serializable_plan,
                                },
                            }
                        )
                active_moved = 0
                active_staged = 0
                active_trashed = 0
                for it in batch_plan.items:
                    if it.is_excluded:
                        continue
                    d_act = (
                        str(it.node_ref.get("duplicate_action") or "").strip().lower()
                        if isinstance(it.node_ref, dict)
                        else ""
                    )
                    p_act = (
                        str(it.node_ref.get("policy_action") or "").strip().lower()
                        if isinstance(it.node_ref, dict)
                        else ""
                    )
                    if p_act == "trash" or d_act == "trash":
                        if flush_trash:
                            active_trashed += 1
                        else:
                            active_staged += 1
                    elif d_act == "stage":
                        active_staged += 1
                    else:
                        active_moved += 1
                return DropBatchResult(
                    session_id=None,
                    resolved_dest=batch_plan.resolved_dest,
                    moved_count=active_moved,
                    staged_count=active_staged,
                    trashed_count=active_trashed,
                    dry_run=True,
                    target_results=target_results,
                )

            lock_targets: list[str] = [grp.source_root for grp in batch_plan.groups]
            if batch_plan.resolved_dest:
                os.makedirs(batch_plan.resolved_dest, exist_ok=True)
                lock_targets.append(batch_plan.resolved_dest)

            primary_session = batch_plan.groups[0].session
            lock_timeout = float(
                getattr(primary_session, "folder_lock_timeout", 10.0) or 10.0
            )
            history_manager = getattr(primary_session, "history_manager", None)
            db = getattr(primary_session, "db", None)

            active_items = [it for it in batch_plan.items if not it.is_excluded]
            batch_base_dir = (
                batch_plan.resolved_dest
                if batch_plan.resolved_dest
                else batch_plan.groups[0].source_root
            )
            batch_roots: list[str] = []
            for r in lock_targets:
                if r and r not in batch_roots:
                    batch_roots.append(r)

            with AppSession.lock_folders(lock_targets, timeout=lock_timeout):
                shared_session_id: str | None = None
                if active_items and history_manager is not None:
                    shared_session_id = history_manager.start_session(batch_base_dir)

                audit_records: list[dict[str, Any]] = []
                failed_count = 0
                cancelled = False
                group_summaries: dict[str, dict[str, Any]] = {}

                try:
                    if batch_plan.resolved_dest and batch_plan.unified_plan is not None:
                        summary = dict(
                            execute_moves(
                                batch_plan.resolved_dest,
                                batch_plan.unified_plan,
                                db,
                                history_manager,
                                self.settings,
                                priority="queue",
                                progress_callback=progress_callback,
                                cancel_check=cancel_check,
                                session_id=shared_session_id,
                                manage_session_lifecycle=False,
                            )
                        )
                        cancelled = bool(summary.get("cancelled"))
                        report = summary.get("audit_report") or {}
                        audit_records.extend(report.get("records") or [])
                        failed_count += int(report.get("mismatch_fail_count", 0) or 0)
                        failed_count += len(report.get("failed_items") or [])
                        for grp in batch_plan.groups:
                            group_summaries[grp.source_root] = dict(summary)
                    else:
                        total_groups = max(1, len(batch_plan.groups))
                        for idx, grp in enumerate(batch_plan.groups):
                            if cancel_check and cancel_check():
                                cancelled = True
                                break

                            def _exec_progress(
                                info: Any, _idx: int = idx, _total: int = total_groups
                            ) -> None:
                                if progress_callback is None:
                                    return
                                if isinstance(info, dict):
                                    scaled = dict(info)
                                    raw_p = float(info.get("progress", 0.0) or 0.0)
                                    scaled["progress"] = (_idx + raw_p) / _total
                                    progress_callback(scaled)
                                else:
                                    progress_callback(info)

                            summary = dict(
                                grp.session.execute_moves(
                                    grp.plan,
                                    acquire_locks=False,
                                    priority="queue",
                                    progress_callback=_exec_progress
                                    if progress_callback is not None
                                    else None,
                                    cancel_check=cancel_check,
                                    session_id=shared_session_id,
                                    manage_session_lifecycle=False,
                                )
                            )
                            group_summaries[grp.source_root] = summary
                            if summary.get("cancelled"):
                                cancelled = True
                                break
                            report = summary.get("audit_report") or {}
                            audit_records.extend(report.get("records") or [])
                            failed_count += int(
                                report.get("mismatch_fail_count", 0) or 0
                            )
                            failed_count += len(report.get("failed_items") or [])
                except Exception:
                    if (
                        shared_session_id
                        and history_manager is not None
                        and hasattr(history_manager, "unwind_session")
                    ):
                        try:
                            history_manager.unwind_session(shared_session_id, db=db)
                        except Exception as unwind_err:  # noqa: BLE001
                            logger.error(
                                "Batch rollback failed for session %s: %s",
                                shared_session_id,
                                unwind_err,
                            )
                    raise

                trash_report: dict[str, Any] | None = None
                if (
                    flush_trash
                    and not cancelled
                    and failed_count == 0
                    and shared_session_id
                    and history_manager is not None
                    and hasattr(history_manager, "flush_staged_duplicates")
                ):
                    trash_report = history_manager.flush_staged_duplicates(
                        session_id=shared_session_id,
                        base_dir=batch_base_dir,
                        trashed_only=flush_trashed_only,
                        trash_fn=trash_fn,
                    )

                real_move_count = 0
                staged_count = 0
                trashed_count = 0
                if shared_session_id and history_manager is not None:
                    ledger = getattr(history_manager, "moves", None)
                    if ledger is not None and hasattr(ledger, "list_moves"):
                        recorded = ledger.list_moves(shared_session_id)
                        for m in recorded:
                            st = getattr(m, "state", None)
                            dst_parts = [
                                p
                                for p in str(getattr(m, "dst", "") or "")
                                .replace("\\", "/")
                                .split("/")
                                if p
                            ]
                            if st == EXPORTED_TO_TRASH:
                                trashed_count += 1
                            elif st in (DONE, VERIFIED):
                                if DUPLICATE_STAGING_DIR in dst_parts:
                                    staged_count += 1
                                else:
                                    real_move_count += 1

                total_recorded = real_move_count + staged_count + trashed_count
                final_session_id = shared_session_id
                if shared_session_id and history_manager is not None:
                    if total_recorded > 0:
                        try:
                            history_manager.complete_session(
                                shared_session_id,
                                metadata={"batch_roots": batch_roots},
                            )
                        except TypeError:
                            history_manager.complete_session(shared_session_id)
                    else:
                        try:
                            hm_db = getattr(history_manager, "db", None)
                            hm_worker = getattr(hm_db, "worker", None)
                            if hm_worker is not None and hasattr(
                                hm_worker, "execute_write"
                            ):
                                hm_worker.execute_write(
                                    lambda: history_manager._transition_session_status(
                                        shared_session_id, "abandoned"
                                    )
                                )
                            elif hasattr(history_manager, "_transition_session_status"):
                                history_manager._transition_session_status(
                                    shared_session_id, "abandoned"
                                )
                        except Exception as ab_err:  # noqa: BLE001
                            logger.debug(
                                "Failed to mark empty batch session %s abandoned: %s",
                                shared_session_id,
                                ab_err,
                            )
                        final_session_id = None

                for grp in batch_plan.groups:
                    restore_rerooted_source_dir(
                        grp.source_root,
                        was_dir=grp.was_dir,
                        re_rooted=grp.re_rooted,
                    )

                target_results = []
                for grp in batch_plan.groups:
                    grp_sum = dict(group_summaries.get(grp.source_root) or {})
                    grp_sum["session_id"] = final_session_id
                    if trash_report is not None:
                        grp_sum["trash_handoff"] = trash_report
                    serializable_plan = _make_json_serializable(grp.plan)
                    targets = grp.target_paths or [grp.source_root]
                    for tgt in targets:
                        target_results.append(
                            {
                                "target": tgt,
                                "base_dir": grp.source_root,
                                "plan": serializable_plan,
                                "summary": _make_json_serializable(grp_sum),
                            }
                        )

                skipped_count = max(
                    0, len(active_items) - total_recorded - failed_count
                )
                return DropBatchResult(
                    session_id=final_session_id,
                    resolved_dest=batch_plan.resolved_dest,
                    moved_count=real_move_count,
                    staged_count=staged_count,
                    trashed_count=trashed_count,
                    failed_count=failed_count,
                    skipped_count=skipped_count,
                    cancelled=cancelled,
                    dry_run=False,
                    target_results=target_results,
                    audit_records=audit_records,
                    trash_handoff=trash_report,
                )
        finally:
            if close_sessions:
                self.close_batch(batch_plan)
