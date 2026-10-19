"""The one sort pipeline every front end runs: discover, extract, fit, plan, then move.

Front ends (CLI, daemon, TUI Drop Zone, demo) build an ``AppSession`` and hand it
to ``SortPipeline`` instead of wiring the extractor, analyzer and mover together
themselves. See issue #708 and the system design's "Target architecture".
"""

import os
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

    def execute(self, plan, **kwargs) -> Any:
        """Move the files in ``plan`` and return the mover's summary."""
        return self.session.execute_moves(plan, **kwargs)

    def run(self, items: Optional[list] = None, *, dry_run: bool = False, **kwargs):
        """Plan, then execute unless ``dry_run``. Returns ``(plan, summary)``.

        A dry run's summary is ``None``.
        """
        plan = self.plan(items, **kwargs)
        if dry_run:
            return plan, None
        return plan, self.execute(plan)
