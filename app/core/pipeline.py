"""The one sort pipeline every front end runs: discover, extract, fit, plan, then move.

Front ends (CLI, daemon, TUI Drop Zone, demo) build an ``AppSession`` and hand it
to ``SortPipeline`` instead of wiring the extractor, analyzer and mover together
themselves. See issue #708 and the system design's "Target architecture".
"""

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


def drop_scope(path: str) -> tuple[str, Optional[list]]:
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

    session_id: Optional[str] = None
    moved: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False
    summary: dict = field(default_factory=dict)

    @classmethod
    def from_summary(cls, summary: Optional[dict], quarantine_dir: str):
        """Build a result from ``execute_moves``'s summary and its audit report."""
        summary = summary or {}
        result = cls(
            session_id=summary.get("session_id"),
            cancelled=bool(summary.get("cancelled")),
            summary=summary,
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
        return result

    @property
    def ok(self) -> bool:
        """True when nothing failed and the sort was not cancelled."""
        return not self.failed and not self.cancelled

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

    @property
    def settings(self):
        """The session's settings."""
        return self.session.settings

    @property
    def base_dir(self) -> str:
        """The folder being sorted."""
        return self.session.base_dir

    def plan(
        self,
        items: Optional[list] = None,
        *,
        progress_callback: Optional[Callable] = None,
        cancel_check: Optional[Callable] = None,
        chunk_size: int = 50,
    ) -> Any:
        """Extract text, fit the analyzer and return the plan. Nothing moves.

        ``items`` defaults to every file under the session's source folder.
        """
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
            self.session.partial_fit(chunk)

        return self.session.generate_sorting_plan()

    def execute(self, plan, **kwargs) -> SortResult:
        """Move the files in ``plan`` and return what happened."""
        summary = self.session.execute_moves(plan, **kwargs)
        quarantine_dir = getattr(
            self.settings, "QUARANTINE_DIR_NAME", "_Compliance_Quarantine"
        )
        return SortResult.from_summary(summary, quarantine_dir)

    def undo(
        self,
        session_id: str,
        *,
        ignore_missing: bool = False,
        selected_step_ids: Optional[Any] = None,
    ) -> SortResult:
        """Move a past sort's files back and return what happened (ADR 0006).

        ``moved`` lists each file put back where it came from, and ``failed``
        each one left in place, such as a file changed since the sort. Raises
        ``ValueError`` for an unknown session.
        """
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
            raise ValueError(f"Session not found: {session_id}")
        moves = [
            m
            for m in history.moves.list_moves(session_id)
            if m.state not in (INTENT, FAILED)
        ]
        if selected_step_ids is not None:
            sel_set = {str(x) for x in selected_step_ids}
            moves = [m for m in moves if str(m.seq) in sel_set]

        result = SortResult(session_id=session_id)
        reason = "Not moved back"
        try:
            kwargs = {"ignore_missing": ignore_missing}
            if selected_step_ids is not None:
                kwargs["selected_step_ids"] = selected_step_ids
            result.summary["restored"] = history.rollback(session_id, **kwargs)
        except (OSError, ValueError) as err:
            # Undo still moves back every file it can before it reports the rest.
            reason = str(err)
            result.summary["error"] = reason
        for move in moves:
            src = safe_relpath(move.src, base_dir).replace("\\", "/")
            if os.path.lexists(move.src):
                result.moved.append(src)
            else:
                result.failed.append((src, reason))
        if "error" in result.summary and not result.failed:
            result.failed.append((session_id, reason))
        return result

    def run(self, items: Optional[list] = None, *, dry_run: bool = False, **kwargs):
        """Plan, then execute unless ``dry_run``. Returns ``(plan, result)``.

        A dry run's result is ``None``.
        """
        plan = self.plan(items, **kwargs)
        if dry_run:
            return plan, None
        return plan, self.execute(plan)
