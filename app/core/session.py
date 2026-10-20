"""Session manager module for encapsulating app state."""

import contextlib
import json
import os
from typing import Any, Dict, Optional, Tuple

from app.config import get_app_dir
from app.core.analyzer import IncrementalAnalyzer
from app.core.domain_contracts import _make_json_serializable


class AppSession:
    """Encapsulates the core business logic, analytics, and database services for a single application run."""

    # Seconds a move waits for another sort of the same folder to finish (ADR 0007).
    folder_lock_timeout = 10.0

    def __init__(
        self, settings, base_dir=None, session_id=None, source_dirs=None, store=None
    ):
        self.settings = settings
        if source_dirs:
            norm_dirs = []
            for d in source_dirs:
                if d:
                    abs_d = os.path.normpath(os.path.abspath(os.path.expanduser(d)))
                    if abs_d not in norm_dirs:
                        norm_dirs.append(abs_d)
            self.source_dirs = norm_dirs
        elif base_dir:
            self.source_dirs = [
                os.path.normpath(os.path.abspath(os.path.expanduser(base_dir)))
            ]
        else:
            self.source_dirs = []

        self.base_dir = self.source_dirs[0] if self.source_dirs else base_dir

        from app.core.path_utils import get_base_path, setup_session_directory

        self.session_id, self.session_dir = setup_session_directory(session_id)

        from app.core.db_worker import DBWorker
        from app.core.store import open_store

        # Every session reads and writes the one app store (ADR 0008).
        self.store = store or open_store()
        self.db_worker = DBWorker()
        self.db = self.store.database(self.db_worker)
        self.cache_manager = self.store.cache(self.db_worker)
        self.history_manager = self.store.history(self.db, self.cache_manager)

        if getattr(self.settings, "AI_CONSENT_GRANTED", None):
            try:
                from app.core.shared_registry import SharedModelRegistry

                jev_engine = SharedModelRegistry.get_instance().get_jev_classifier()
                jev_engine.set_database(str(self.store.path), self.db_worker)
            except Exception:
                pass

        try:
            from app.core.ledger import TransactionLedger

            ledger = TransactionLedger()
            ledger.reconcile_incomplete_transactions(db=self.db)
        except Exception:
            pass

        base_path = get_base_path(__file__)

        local_model_path = os.path.join(base_path, "offline_bundle", "model")
        user_model_path = str(get_app_dir() / "model")

        active_model_path = None
        from app.core.path_utils import get_meipass_dir

        mei_dir = get_meipass_dir()
        if mei_dir:
            mei_bundle_path = os.path.join(mei_dir, "offline_bundle", "model")
            if os.path.exists(mei_bundle_path):
                active_model_path = mei_bundle_path

        if not active_model_path:
            if os.path.exists(local_model_path):
                active_model_path = local_model_path
            elif os.path.exists(user_model_path):
                active_model_path = user_model_path

        model_path = active_model_path if self.settings.AI_CONSENT_GRANTED else None
        configured_strat = getattr(self.settings, "SORTING_STRATEGY", "default")
        if configured_strat in ("clinical_tmf", "clinical_isf"):
            strategy_name = configured_strat
            from app.core.analyzer_strategies import clustering_registry

            strat = clustering_registry.get_strategy(strategy_name)
            if strat and hasattr(strat, "smart_renaming"):
                strat.smart_renaming = getattr(
                    self.settings, "CLINICAL_SMART_RENAMING", False
                )
                strat.generate_audit_report = getattr(
                    self.settings, "CLINICAL_GENERATE_AUDIT_REPORT", True
                )
                strat.base_dir = self.base_dir
        else:
            strategy_name = (
                "generative"
                if getattr(self.settings, "AI_ASSISTED_NAMING", False)
                else "default"
            )

        self.analyzer = IncrementalAnalyzer(
            self.settings.MAX_FOLDERS,
            self.settings.STOP_WORDS,
            self.db,
            strategy_name=strategy_name,
            model_path=model_path,
        )

    async def process_items_async(
        self, items_to_sort, cancel_check, progress_callback=None
    ):
        """Build corpus asynchronous generator for files, yielded file-by-file sequentially."""
        if not self.base_dir and not self.source_dirs:
            return
        from app.core.extractor import build_corpus_generator_async

        async for item, text, file_hash, was_skipped in build_corpus_generator_async(
            self.base_dir,
            items_to_sort,
            db=self.db,
            cancel_check=cancel_check,
            settings=self.settings,
            progress_callback=progress_callback,
            source_dirs=self.source_dirs,
        ):
            yield item, text, file_hash, was_skipped

    def partial_fit(self, chunk):
        """Incrementally train the analyzer."""
        if not self.base_dir and not self.source_dirs:
            return
        self.analyzer.partial_fit(self.base_dir, chunk, self.settings)

    def generate_sorting_plan(
        self,
        fast_path_only: bool = False,
        target_paths: Optional[list[str]] = None,
        jev_results: Optional[Dict[str, Any]] = None,
    ):
        """Generate sorting plan from analyzer."""
        if not self.base_dir and not self.source_dirs:
            return {}
        locked = {}
        dirs_to_check = (
            self.source_dirs
            if self.source_dirs
            else ([self.base_dir] if self.base_dir else [])
        )
        for s_dir in dirs_to_check:
            _, lk, _, _ = self.cache_manager.load_cache(s_dir)
            if lk:
                locked.update(lk)
        return self.analyzer.generate_sorting_plan(
            self.base_dir,
            self.settings,
            locked_files=locked,
            fast_path_only=fast_path_only,
            target_paths=target_paths,
            jev_results=jev_results,
            source_dirs=self.source_dirs,
        )

    def rollback(self, session_id, ignore_missing=False):
        """Rollback a past session."""
        self.history_manager.rollback(session_id, ignore_missing=ignore_missing)

    def execute_moves(
        self,
        plan,
        resume=False,
        cancel_check=None,
        chunk_size=None,
        progress_callback=None,
        **kwargs,
    ):
        """Execute move operations using asynchronous chunked worker pipeline.

        Args:
            plan: The file movement plan dict.
            resume: Whether to resume a previously halted move session.
            cancel_check: Optional callable that checks if cancellation was requested.
            chunk_size: Optional chunk size for worker execution.
            progress_callback: Optional progress callback for emitting status updates.
            **kwargs: Additional arguments passed to execute_moves.

        Returns
        -------
            dict: Summary dictionary containing move execution results and statistics.
        """
        if not self.base_dir and not self.source_dirs:
            return {}

        plan_path = self.session_dir / "plan.json"
        serializable_plan = _make_json_serializable(plan)
        with open(plan_path, "w") as f:
            json.dump(serializable_plan, f)

        from app.core.mover import execute_moves

        with self._lock_folders():
            return execute_moves(
                self.base_dir,
                plan,
                self.db,
                self.history_manager,
                self.settings,
                resume=resume,
                cancel_check=cancel_check,
                chunk_size=chunk_size,
                progress_callback=progress_callback,
                source_dirs=self.source_dirs,
                **kwargs,
            )

    def retry_failed_moves(self, failed_items, progress_callback=None, **kwargs):
        """Re-attempt move operations for failed items."""
        from app.core.mover import retry_failed_moves

        with self._lock_folders():
            return retry_failed_moves(
                self.base_dir,
                failed_items,
                db=self.db,
                history_manager=self.history_manager,
                runtime_settings=self.settings,
                progress_callback=progress_callback,
                **kwargs,
            )

    def _lock_folders(self) -> contextlib.ExitStack:
        """Hold a FolderLock on every source folder while files move."""
        from app.core.folder_lock import FolderLock

        stack = contextlib.ExitStack()
        folders = self.source_dirs or ([self.base_dir] if self.base_dir else [])
        try:
            for folder in sorted(folders):
                stack.enter_context(
                    FolderLock(folder, timeout=self.folder_lock_timeout)
                )
        except BaseException:
            stack.close()
            raise
        return stack

    def close(self):
        """Cleanup session directory."""
        if hasattr(self, "analyzer") and self.analyzer:
            self.analyzer.terminate()
        if hasattr(self, "db_worker") and self.db_worker:
            self.db_worker.stop()
        try:
            from app.core.shared_registry import SharedModelRegistry, SharedWorkerPool

            SharedWorkerPool.shutdown_instance(wait=False)
            SharedModelRegistry.get_instance().unload_all_models()
        except Exception:
            pass
        try:
            from app.core.db_conn import clear_connection_cache

            clear_connection_cache(only_current_and_inactive=False)
        except Exception:
            pass
        if self.session_dir and os.path.exists(self.session_dir):
            from app.core.resilient_file_ops import resilient_rmtree

            resilient_rmtree(self.session_dir, ignore_errors=True)


def split_plan_phases(plan: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Split plan items into Phase 1 (deterministic/fast path) and Phase 2 (AI/clustering/slow path)."""
    fast_plan: Dict[str, Any] = {}
    slow_plan: Dict[str, Any] = {}

    FAST_ROUTES = {
        "keyword",
        "override",
        "rule",
        "extension",
        "jev_classifier",
        "preset",
    }

    def process_node(
        node: Any, fast_target: Dict[str, Any], slow_target: Dict[str, Any]
    ):
        if not isinstance(node, dict):
            return
        for key, val in node.items():
            if (
                isinstance(val, dict)
                and "__type__" in val
                and val["__type__"] == "file"
            ):
                routed_by = val.get("routed_by")
                if routed_by in FAST_ROUTES:
                    fast_target[key] = val
                else:
                    slow_target[key] = val
            elif isinstance(val, dict):
                sub_fast: Dict[str, Any] = {}
                sub_slow: Dict[str, Any] = {}
                process_node(val, sub_fast, sub_slow)
                if sub_fast:
                    fast_target[key] = sub_fast
                if sub_slow:
                    slow_target[key] = sub_slow
            else:
                slow_target[key] = val

    process_node(plan, fast_plan, slow_plan)
    return fast_plan, slow_plan
