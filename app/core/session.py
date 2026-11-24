"""Session manager module for encapsulating app state."""

import contextlib
import json
import os
from typing import Any

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

        self.session_id, self.session_dir = setup_session_directory(
            str(session_id) if session_id else ""
        )

        from app.core.pipeline import PipelineLifecycleState
        from app.core.services import acquire_services

        self.lifecycle_state = PipelineLifecycleState.CREATED
        self.pipeline = None

        # Every session reads and writes the one app store (ADR 0008).
        self._service_lease = acquire_services(store)
        self.store = self._service_lease.store
        self.db_worker = self._service_lease.worker
        self.db = self._service_lease.db
        self.cache_manager = self._service_lease.cache
        self.history_manager = self._service_lease.history
        self._closed = False

        try:
            if getattr(self.settings, "AI_CONSENT_GRANTED", None):
                try:
                    from app.core.shared_registry import SharedModelRegistry

                    jev_engine = SharedModelRegistry.get_instance().get_jev_classifier()
                    jev_engine.set_database(str(self.store.path), self.db_worker)
                except Exception:
                    pass

            try:
                self.history_manager.moves.reconcile_incomplete_transactions(db=self.db)
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
                    setattr(
                        strat,
                        "smart_renaming",
                        bool(getattr(self.settings, "CLINICAL_SMART_RENAMING", False)),
                    )
                    setattr(
                        strat,
                        "generate_audit_report",
                        bool(
                            getattr(
                                self.settings, "CLINICAL_GENERATE_AUDIT_REPORT", True
                            )
                        ),
                    )
                    setattr(strat, "base_dir", self.base_dir)
            else:
                is_generative = configured_strat == "generative" or bool(
                    getattr(self.settings, "AI_ASSISTED_NAMING", False)
                )
                strategy_name = "generative" if is_generative else "default"
                if is_generative:
                    from app.core.analyzer_strategies import clustering_registry

                    gen_strat = clustering_registry.get_strategy("generative")
                    gen_model_path = None
                    if self.settings.AI_CONSENT_GRANTED:
                        env_gen_path = os.environ.get("GENERATIVE_NAMING_PATH")
                        gen_candidates = []
                        if env_gen_path:
                            gen_candidates.append(env_gen_path)
                        if mei_dir:
                            gen_candidates.append(
                                os.path.join(
                                    mei_dir, "offline_bundle", "generative_naming"
                                )
                            )
                        gen_candidates.append(
                            os.path.join(
                                base_path, "offline_bundle", "generative_naming"
                            )
                        )
                        gen_candidates.append(str(get_app_dir() / "generative_naming"))
                        gen_candidates.append(
                            str(get_app_dir() / "models" / "generative_naming")
                        )
                        for cand in gen_candidates:
                            if cand and os.path.exists(cand):
                                gen_model_path = cand
                                break
                        if (
                            not gen_model_path
                            and active_model_path
                            and os.path.isdir(active_model_path)
                        ):
                            try:
                                if any(
                                    fn.endswith(".gguf")
                                    for fn in os.listdir(active_model_path)
                                ):
                                    gen_model_path = active_model_path
                            except OSError:
                                pass
                    if gen_strat is not None and hasattr(gen_strat, "model_path"):
                        setattr(gen_strat, "model_path", gen_model_path)

            self.analyzer = IncrementalAnalyzer(
                self.settings.MAX_FOLDERS,
                self.settings.STOP_WORDS,
                self.db,
                strategy_name=strategy_name,
                model_path=model_path,
            )
        except BaseException:
            self._service_lease.close()
            raise

    async def process_items_async(
        self, items_to_sort, cancel_check, progress_callback=None
    ):
        """Build corpus asynchronous generator for files, yielded file-by-file sequentially."""
        effective_base_dir = self.base_dir or (
            self.source_dirs[0] if self.source_dirs else None
        )
        if not effective_base_dir:
            return
        from app.core.extractor import build_corpus_generator_async

        async for item, text, file_hash, was_skipped in build_corpus_generator_async(
            effective_base_dir,
            items_to_sort,
            db=self.db,
            cancel_check=cancel_check,
            settings=self.settings,
            progress_callback=progress_callback,
            source_dirs=self.source_dirs,
        ):
            yield item, text, file_hash, was_skipped

    def transition_to(
        self,
        target_state: Any,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Validate and transition lifecycle state, delegating to pipeline if attached."""
        pipeline = getattr(self, "pipeline", None)
        if pipeline is not None and hasattr(pipeline, "transition_to"):
            pipeline.transition_to(target_state, context=context)
            return

        from app.core.pipeline import (
            _ALLOWED_TRANSITIONS,
            IllegalStateTransitionError,
        )

        current_state = getattr(self, "lifecycle_state", None)
        if target_state == current_state:
            return

        allowed = _ALLOWED_TRANSITIONS.get(current_state, set())
        if target_state not in allowed:
            state_val = getattr(current_state, "value", str(current_state))
            target_val = getattr(target_state, "value", str(target_state))
            raise IllegalStateTransitionError(
                f"Illegal state transition from '{state_val}' to '{target_val}'."
            )
        self.lifecycle_state = target_state

    def partial_fit(self, chunk):
        """Incrementally train the analyzer."""
        if hasattr(self, "lifecycle_state"):
            from app.core.pipeline import (
                IllegalStateTransitionError,
                PipelineLifecycleState,
            )

            if self.lifecycle_state in (
                PipelineLifecycleState.EXECUTING,
                PipelineLifecycleState.ROLLING_BACK,
            ):
                raise IllegalStateTransitionError(
                    f"Cannot fit analyzer while execution is in progress (current state: '{self.lifecycle_state.value}')."
                )
            if self.lifecycle_state == PipelineLifecycleState.CREATED:
                self.transition_to(PipelineLifecycleState.PLANNING)
        effective_base_dir = self.base_dir or (
            self.source_dirs[0] if self.source_dirs else None
        )
        if not effective_base_dir:
            return
        self.analyzer.partial_fit(effective_base_dir, chunk, self.settings)

    def generate_sorting_plan(
        self,
        fast_path_only: bool = False,
        target_paths: list[str] | None = None,
        jev_results: dict[str, Any] | None = None,
        existing_folders: list[str] | None = None,
        dest_dir: str | None = None,
    ):
        """Generate sorting plan from analyzer."""
        if hasattr(self, "lifecycle_state"):
            from app.core.pipeline import (
                IllegalStateTransitionError,
                PipelineLifecycleState,
            )

            if self.lifecycle_state in (
                PipelineLifecycleState.EXECUTING,
                PipelineLifecycleState.ROLLING_BACK,
            ):
                raise IllegalStateTransitionError(
                    f"Cannot generate plan while execution is in progress (current state: '{self.lifecycle_state.value}')."
                )
        effective_base_dir = self.base_dir or (
            self.source_dirs[0] if self.source_dirs else None
        )
        if not effective_base_dir:
            return {}
        locked = {}
        dirs_to_check = (
            self.source_dirs
            if self.source_dirs
            else ([effective_base_dir] if effective_base_dir else [])
        )
        for s_dir in dirs_to_check:
            _, lk, _, _ = self.cache_manager.load_cache(s_dir)
            if lk:
                locked.update(lk)
        plan = self.analyzer.generate_sorting_plan(
            effective_base_dir,
            self.settings,
            locked_files=locked,
            fast_path_only=fast_path_only,
            target_paths=target_paths,
            jev_results=jev_results,
            source_dirs=self.source_dirs,
            existing_folders=existing_folders,
            dest_dir=dest_dir,
        )
        if (
            hasattr(self, "lifecycle_state")
            and self.lifecycle_state != PipelineLifecycleState.EXECUTING
        ):
            self.transition_to(PipelineLifecycleState.PLANNED)
        return plan

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
        pipeline=None,
        **kwargs,
    ):
        """Execute move operations using asynchronous chunked worker pipeline.

        Args:
            plan: The file movement plan dict.
            resume: Whether to resume a previously halted move session.
            cancel_check: Optional callable that checks if cancellation was requested.
            chunk_size: Optional chunk size for worker execution.
            progress_callback: Optional progress callback for emitting status updates.
            pipeline: Optional SortPipeline instance coordinating lifecycle state.
            **kwargs: Additional arguments passed to execute_moves.

        Returns
        -------
            dict: Summary dictionary containing move execution results and statistics.
        """
        effective_base_dir = self.base_dir or (
            self.source_dirs[0] if self.source_dirs else None
        )
        if not effective_base_dir:
            return {}

        effective_pipeline = pipeline or getattr(self, "pipeline", None)
        if (
            effective_pipeline
            and getattr(self, "pipeline", None) is not effective_pipeline
        ):
            self.pipeline = effective_pipeline

        if hasattr(self, "lifecycle_state"):
            from app.core.pipeline import (
                IllegalStateTransitionError,
                PipelineLifecycleState,
            )

            if (
                self.lifecycle_state
                not in (
                    PipelineLifecycleState.PLANNED,
                    PipelineLifecycleState.EXECUTING,
                )
                and not resume
                and not kwargs.get("force")
                and plan
            ):
                raise IllegalStateTransitionError(
                    f"Cannot execute moves from state '{self.lifecycle_state.value}'; session must be in 'planned' state."
                )
            if self.lifecycle_state == PipelineLifecycleState.PLANNED or (
                self.lifecycle_state != PipelineLifecycleState.EXECUTING and plan
            ):
                self.transition_to(PipelineLifecycleState.EXECUTING)

        plan_path = self.session_dir / "plan.json"
        serializable_plan = _make_json_serializable(plan)
        with open(plan_path, "w") as f:
            json.dump(serializable_plan, f)

        from app.core.mover import execute_moves

        move_kwargs: dict[str, Any] = dict(kwargs)
        acquire_locks = bool(move_kwargs.pop("acquire_locks", True))
        if chunk_size is not None:
            move_kwargs["chunk_size"] = chunk_size

        lock_ctx = self._lock_folders() if acquire_locks else contextlib.nullcontext()
        with lock_ctx:
            try:
                summary = execute_moves(
                    effective_base_dir,
                    plan,
                    self.db,
                    self.history_manager,
                    self.settings,
                    resume=resume,
                    cancel_check=cancel_check,
                    progress_callback=progress_callback,
                    source_dirs=self.source_dirs,
                    pipeline=effective_pipeline,
                    **move_kwargs,
                )
                if hasattr(self, "lifecycle_state"):
                    from app.core.pipeline import PipelineLifecycleState

                    if summary.get("cancelled"):
                        self.transition_to(PipelineLifecycleState.CANCELLED)
                    elif summary.get("error"):
                        self.transition_to(PipelineLifecycleState.FAILED)
                    else:
                        self.transition_to(PipelineLifecycleState.COMPLETED)
                return summary
            except Exception:
                if hasattr(self, "lifecycle_state"):
                    from app.core.pipeline import PipelineLifecycleState

                    try:
                        self.transition_to(PipelineLifecycleState.FAILED)
                    except Exception:
                        pass
                raise

    def retry_failed_moves(self, failed_items, progress_callback=None, **kwargs):
        """Re-attempt move operations for failed items."""
        effective_base_dir = self.base_dir or (
            self.source_dirs[0] if self.source_dirs else None
        )
        if not effective_base_dir:
            return {}
        from app.core.mover import retry_failed_moves

        with self._lock_folders():
            return retry_failed_moves(
                effective_base_dir,
                failed_items,
                db=self.db,
                history_manager=self.history_manager,
                runtime_settings=self.settings,
                progress_callback=progress_callback,
                **kwargs,
            )

    @staticmethod
    def lock_folders(
        folders: list[str] | tuple[str, ...] | set[str],
        timeout: float = 0.0,
    ) -> contextlib.ExitStack:
        """Acquire exclusive FolderLocks across unique folders in deterministic sorted order."""
        from app.core.folder_lock import FolderLock

        stack = contextlib.ExitStack()
        deduped: dict[str, str] = {}
        for folder in folders:
            if not folder:
                continue
            key = os.path.normcase(os.path.realpath(os.path.abspath(str(folder))))
            deduped.setdefault(key, str(folder))
        try:
            for key in sorted(deduped):
                stack.enter_context(FolderLock(deduped[key], timeout=timeout))
        except BaseException:
            stack.close()
            raise
        return stack

    def _lock_folders(self) -> contextlib.ExitStack:
        """Hold a FolderLock on every source folder while files move."""
        folders = self.source_dirs or ([self.base_dir] if self.base_dir else [])
        return self.lock_folders(folders, timeout=self.folder_lock_timeout)

    def close(self):
        """Release session services without stopping another active consumer."""
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            if hasattr(self, "analyzer") and self.analyzer:
                self.analyzer.terminate()
        finally:
            if hasattr(self, "_service_lease"):
                self._service_lease.close()
            if self.session_dir and os.path.exists(self.session_dir):
                from app.core.resilient_file_ops import resilient_rmtree

                resilient_rmtree(self.session_dir, ignore_errors=True)


def split_plan_phases(plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split plan items into Phase 1 (deterministic/fast path) and Phase 2 (AI/clustering/slow path)."""
    fast_plan: dict[str, Any] = {}
    slow_plan: dict[str, Any] = {}

    FAST_ROUTES = {
        "keyword",
        "override",
        "rule",
        "extension",
        "jev_classifier",
        "preset",
    }

    def process_node(
        node: Any, fast_target: dict[str, Any], slow_target: dict[str, Any]
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
                sub_fast: dict[str, Any] = {}
                sub_slow: dict[str, Any] = {}
                process_node(val, sub_fast, sub_slow)
                if sub_fast:
                    fast_target[key] = sub_fast
                if sub_slow:
                    slow_target[key] = sub_slow
            else:
                slow_target[key] = val

    process_node(plan, fast_plan, slow_plan)
    return fast_plan, slow_plan
