"""Session manager module for encapsulating app state."""

import contextlib
import json
import logging
import os
import threading
from typing import Any

from app.config import get_app_dir
from app.core.analyzer import IncrementalAnalyzer
from app.core.domain_contracts import _make_json_serializable

logger = logging.getLogger(__name__)


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
        self._lifecycle_lock = threading.RLock()

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
                    logger.warning(
                        "Failed to attach database to JEV classifier for session %s",
                        self.session_id,
                        exc_info=True,
                    )

            try:
                self.history_manager.moves.reconcile_incomplete_transactions(db=self.db)
            except Exception:
                logger.warning(
                    "Failed to reconcile incomplete move transactions during startup for session %s",
                    self.session_id,
                    exc_info=True,
                )

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

        lock = getattr(self, "_lifecycle_lock", None) or contextlib.nullcontext()
        with lock:
            current_state = getattr(self, "lifecycle_state", None)
            if target_state == current_state:
                return

            allowed = _ALLOWED_TRANSITIONS.get(current_state, set())
            if target_state not in allowed:
                raise IllegalStateTransitionError(
                    f"Illegal state transition from '{current_state}' to '{target_state}'."
                )
            self.lifecycle_state = target_state

    def partial_fit(self, chunk):
        """Incrementally train the analyzer."""
        from app.core.pipeline import (
            _ACTIVE_PIPELINE,
            IllegalStateTransitionError,
            PipelineLifecycleState,
        )

        active_pipeline = _ACTIVE_PIPELINE.get()
        if active_pipeline is not None:
            if active_pipeline.state in (
                PipelineLifecycleState.EXECUTING,
                PipelineLifecycleState.ROLLING_BACK,
            ):
                raise IllegalStateTransitionError(
                    f"Cannot fit analyzer while execution is in progress (current state: '{active_pipeline.state.value}')."
                )
        elif hasattr(self, "lifecycle_state"):
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
        self.analyzer.partial_fit(effective_base_dir, chunk)

    def generate_sorting_plan(
        self,
        fast_path_only: bool = False,
        target_paths: list[str] | None = None,
        jev_results: dict[str, Any] | None = None,
        existing_folders: list[str] | None = None,
        dest_dir: str | None = None,
    ):
        """Generate sorting plan from analyzer."""
        from app.core.pipeline import (
            _ACTIVE_PIPELINE,
            IllegalStateTransitionError,
            PipelineLifecycleState,
        )

        active_pipeline = _ACTIVE_PIPELINE.get()
        if active_pipeline is not None:
            if active_pipeline.state in (
                PipelineLifecycleState.EXECUTING,
                PipelineLifecycleState.ROLLING_BACK,
            ):
                raise IllegalStateTransitionError(
                    f"Cannot generate plan while execution is in progress (current state: '{active_pipeline.state.value}')."
                )
        elif hasattr(self, "lifecycle_state"):
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
        manual_folders: set[str] = set()
        dirs_to_check = (
            self.source_dirs
            if self.source_dirs
            else ([effective_base_dir] if effective_base_dir else [])
        )
        for s_dir in dirs_to_check:
            _, lk, _, mf = self.cache_manager.load_cache(s_dir)
            if lk:
                locked.update(lk)
            if isinstance(mf, (set, list)):
                manual_folders.update(str(item) for item in mf if item)
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
        if manual_folders:
            from app.core.path_utils import (
                resolve_plan_folder_dict,
                validate_target_path,
            )

            for folder_name in sorted(manual_folders):
                try:
                    validate_target_path(folder_name)
                except ValueError:
                    continue
                clean_folder = "/".join(
                    p
                    for p in str(folder_name).replace("\\", "/").strip("/").split("/")
                    if p
                )
                if clean_folder:
                    resolve_plan_folder_dict(plan, clean_folder, create=True)
        if (
            active_pipeline is None
            and hasattr(self, "lifecycle_state")
            and self.lifecycle_state != PipelineLifecycleState.EXECUTING
        ):
            self.transition_to(PipelineLifecycleState.PLANNED)
        return plan

    def rollback(self, session_id, ignore_missing=False):
        """Rollback a past session."""
        self.history_manager.rollback(session_id)

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
        if pipeline is not None and getattr(self, "pipeline", None) is not pipeline:
            self.pipeline = pipeline

        from app.core.pipeline import (
            IllegalStateTransitionError,
            PipelineLifecycleState,
        )

        def _transition_exec_state(target_state: PipelineLifecycleState) -> None:
            if effective_pipeline is not None and hasattr(
                effective_pipeline, "transition_to"
            ):
                effective_pipeline.transition_to(target_state)
            elif hasattr(self, "lifecycle_state"):
                self.transition_to(target_state)

        if effective_pipeline is not None and hasattr(effective_pipeline, "state"):
            check_state = effective_pipeline.state
            if (
                check_state
                not in (
                    PipelineLifecycleState.PLANNED,
                    PipelineLifecycleState.EXECUTING,
                )
                and not resume
                and not kwargs.get("force")
                and plan
            ):
                raise IllegalStateTransitionError(
                    f"Cannot execute moves from state '{check_state.value}'; session must be in 'planned' state."
                )
            if check_state == PipelineLifecycleState.PLANNED or (
                check_state != PipelineLifecycleState.EXECUTING and plan
            ):
                _transition_exec_state(PipelineLifecycleState.EXECUTING)
        elif hasattr(self, "lifecycle_state"):
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
                _transition_exec_state(PipelineLifecycleState.EXECUTING)

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
                    pipeline=effective_pipeline,
                    **move_kwargs,
                )
                if pipeline is None and (
                    effective_pipeline is not None or hasattr(self, "lifecycle_state")
                ):
                    if summary.get("cancelled"):
                        _transition_exec_state(PipelineLifecycleState.CANCELLED)
                    elif (
                        summary.get("error")
                        or summary.get("lifecycle_state")
                        == PipelineLifecycleState.FAILED
                        or bool((summary.get("audit_report") or {}).get("failed_items"))
                    ):
                        _transition_exec_state(PipelineLifecycleState.FAILED)
                    else:
                        _transition_exec_state(PipelineLifecycleState.COMPLETED)
                return summary
            except Exception:
                if pipeline is None and (
                    effective_pipeline is not None or hasattr(self, "lifecycle_state")
                ):
                    try:
                        _transition_exec_state(PipelineLifecycleState.FAILED)
                    except Exception:
                        logger.warning(
                            "Failed to transition session %s lifecycle state to FAILED",
                            getattr(self, "session_id", "<unknown>"),
                            exc_info=True,
                        )
                raise

    def retry_failed_moves(
        self,
        failed_items: list[dict[str, Any]],
        progress_callback: Any = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Re-attempt move operations for failed items using session-managed DBWorker and MoveLedger."""
        effective_base_dir = self.base_dir or (
            self.source_dirs[0] if self.source_dirs else None
        )
        if not effective_base_dir:
            return {}
        from app.core.move_retry import execute_journaled_retry

        with self._lock_folders():
            return execute_journaled_retry(
                effective_base_dir,
                failed_items,
                progress_callback=progress_callback,
                db=self.db,
                history_manager=self.history_manager,
                runtime_settings=self.settings,
                session_id=session_id,
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
