"""History, undo and interrupted-session presentation and dispatch."""

import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from textual import work

from app.ui.modals.session import (
    HistoryModal,
    MultiSessionRecoveryModal,
    RollbackConfirmModal,
    RollbackErrorModal,
    SessionRecoveryModal,
)

logger = logging.getLogger(__name__)


class SessionHistoryMixin:
    """Present session actions while core services own persistence and recovery."""

    if TYPE_CHECKING:
        # Host contract only: these declarations add no runtime overrides to
        # Textual's MRO and keep extraction from creating new type-warning debt.
        base_dir: str | None
        settings: Any
        app_session: Any
        plan: Any
        push_screen: Callable[..., Any]
        call_from_thread: Callable[..., Any]
        action_scan_directory: Callable[[], None]
        action_execute_sort: Callable[[], None]
        _is_text_control_focused: Callable[..., bool]

        def announce(
            self, message: str, priority: str = "polite", help_url: str | None = None
        ) -> str:
            """Describe the host's accessibility announcement contract."""
            ...

        def rebuild_tree(
            self, search_query: str | None = None, run_validation: bool = True
        ) -> None:
            """Describe the host's tree refresh contract."""
            ...

    @work
    async def check_abandoned_sessions(self) -> None:
        """Scan for abandoned sessions on startup and prompt for recovery if detected."""
        try:
            import asyncio

            from app.core.session_catalog import find_abandoned_sessions

            abandoned = await asyncio.to_thread(find_abandoned_sessions)
            if not abandoned:
                return

            if len(abandoned) == 1:
                session_info = abandoned[0]

                def on_recovery_choice(choice: Optional[str]) -> None:
                    if not choice:
                        return
                    if choice == "resume":
                        self._do_recovery_resume(session_info)
                    elif choice == "rollback":
                        self._do_recovery_rollback(session_info)
                    elif choice == "clean":
                        self._do_recovery_clean(session_info)

                self.push_screen(SessionRecoveryModal(session_info), on_recovery_choice)
            else:

                def on_multi_recovery_choice(choice: Any) -> None:
                    if not choice:
                        return
                    action = getattr(choice, "action", choice)
                    target_session = getattr(choice, "session", None)
                    if isinstance(choice, dict):
                        action = choice.get("action")
                        target_session = choice.get("session")

                    if action == "clean_all":
                        sessions_to_clean = getattr(choice, "sessions", abandoned)
                        for sess in sessions_to_clean:
                            self._do_recovery_clean(sess)
                    elif action == "rollback_all":
                        sessions_to_rollback = getattr(choice, "sessions", abandoned)
                        for sess in sessions_to_rollback:
                            self._do_recovery_rollback(sess)
                        if self.base_dir:
                            self.action_scan_directory()
                            self.rebuild_tree()
                    elif action == "resume" and target_session:
                        self._do_recovery_resume(target_session)
                    elif action == "rollback" and target_session:
                        self._do_recovery_rollback(target_session)
                    elif action == "clean" and target_session:
                        self._do_recovery_clean(target_session)

                self.push_screen(
                    MultiSessionRecoveryModal(abandoned, settings=self.settings),
                    on_multi_recovery_choice,
                )
        except Exception as e:
            logger.error(f"Error checking abandoned sessions: {e}")

    def _do_recovery_resume(self, session_info: dict[str, Any]) -> None:
        """Resume interrupted sorting operation."""
        import json

        from app.core.session import AppSession

        base_dir = session_info.get("base_dir") or self.base_dir
        if base_dir:
            self.base_dir = base_dir

        self.app_session = AppSession(
            self.settings, self.base_dir, session_id=session_info.get("session_id")
        )

        plan_path = session_info.get("plan_path")
        if plan_path and os.path.exists(plan_path):
            try:
                with open(plan_path, "r", encoding="utf-8") as f:
                    self.plan = json.load(f)
            except Exception:
                self.plan = self.app_session.generate_sorting_plan()
        else:
            self.plan = self.app_session.generate_sorting_plan()

        self.rebuild_tree()
        self.announce(
            f"Resuming session '{session_info.get('session_id')}'. Executing pending moves..."
        )
        self.action_execute_sort()

    def _do_recovery_rollback(self, session_info: dict[str, Any]) -> None:
        """Rollback interrupted sorting operation."""
        from app.core.session_catalog import roll_back_abandoned_session

        session_id = session_info.get("session_id")
        info = {
            **session_info,
            "base_dir": session_info.get("base_dir") or self.base_dir,
        }
        if roll_back_abandoned_session(self.settings, info):
            self.announce(f"Rolled back session '{session_id}'.")
        else:
            self.announce(
                f"Session recovery failed for '{session_id}': write/permission error. Session files and safety storage preserved."
            )

        if self.base_dir:
            self.action_scan_directory()

    def _do_recovery_clean(self, session_info: dict[str, Any]) -> None:
        """Leave an abandoned session as it is and stop offering to recover it."""
        from app.core.session_catalog import forget_abandoned_session

        forget_abandoned_session(session_info)
        self.announce(f"Cleaned session files for '{session_info.get('session_id')}'.")

    def action_open_history(self) -> None:
        """Trigger session history view and audit log export modal screen."""
        if self._is_text_control_focused(allow_tree_search=True):
            return
        from app.core.session_catalog import list_sessions

        sessions = list_sessions()

        def handle_history_modal_result(result: Optional[dict[str, Any]]) -> None:
            if not result:
                return
            session_id = result["session_id"]
            output_path = result["output_path"]
            fmt = result["format"]

            try:
                from app.core.services import acquire_services

                with acquire_services() as services:
                    export_res = services.history.export_audit_log(
                        output_path=output_path,
                        session_id=session_id
                        if session_id not in ("none", "all")
                        else None,
                        format=fmt,
                        base_dir=self.base_dir,
                    )
                cnt = export_res.get("count", 0)
                out_p = export_res.get("output_path", output_path)
                msg = (
                    f"Audit log exported ({fmt.upper()}) to '{out_p}' ({cnt} records)."
                )
                self.announce(msg)
                from app.ui.notifications import notify

                notify(msg, type="info")
            except Exception as e:
                logger.error(f"Error exporting audit log from TUI: {e}")
                self.announce(f"Export error: {e}")
                from app.ui.notifications import notify

                notify(f"Export error: {e}", type="error")

        self.push_screen(
            HistoryModal(sessions=sessions, base_dir=self.base_dir),
            handle_history_modal_result,
        )

    def action_undo_last_sort(self) -> None:
        """Trigger instant undo rollback for the most recent completed sorting session."""
        if self._is_text_control_focused(allow_tree_search=True):
            return
        from app.core.session_catalog import list_sessions

        sessions = list_sessions()
        target_sess = None
        for s in sessions:
            if str(s.get("status", "")).lower() == "completed":
                target_sess = s
                break

        if not target_sess:
            self.announce("No completed sorting session available to undo.")
            return

        def handle_confirm_result(result: Optional[dict[str, Any]]) -> None:
            if not result or not result.get("confirmed"):
                return
            sid = result["session_id"]
            ignore_missing = result.get("ignore_missing", False)
            selected_step_ids = result.get("selected_step_ids")
            self.run_undo_worker(sid, ignore_missing, selected_step_ids)

        self.push_screen(
            RollbackConfirmModal(session_info=target_sess),
            handle_confirm_result,
        )

    @work(thread=True)
    def run_undo_worker(
        self,
        session_id: str,
        ignore_missing: bool = False,
        selected_step_ids: Optional[list[Any]] = None,
    ) -> None:
        """Execute session rollback restoration asynchronously in background worker thread."""
        try:
            from app.core.history import open_session_history

            kwargs: dict[str, Any] = {"ignore_missing": ignore_missing}
            if selected_step_ids is not None:
                kwargs["selected_step_ids"] = selected_step_ids

            with open_session_history() as history_mgr:
                restored_count = history_mgr.rollback(session_id, **kwargs)

            if hasattr(self, "call_from_thread"):
                self.call_from_thread(self._on_undo_success, session_id, restored_count)
            else:
                self._on_undo_success(session_id, restored_count)
        except Exception as err:
            logger.error(f"Error rolling back session {session_id}: {err}")
            if hasattr(self, "call_from_thread"):
                self.call_from_thread(self._on_undo_failure, session_id, err)
            else:
                self._on_undo_failure(session_id, err)

    def _on_undo_success(self, session_id: str, restored_count: int) -> None:
        """Handle successful session rollback completion on main thread."""
        msg = f"Session {session_id[:8]}... successfully rolled back ({restored_count} file(s) restored)."
        self.announce(msg)
        try:
            from app.ui.notifications import notify

            notify(msg, type="info")
        except Exception:
            pass

    def _on_undo_failure(self, session_id: str, err: Exception) -> None:
        """Handle session rollback failure on main thread."""
        msg = f"Rollback failed for session {session_id[:8]}...: {err}"
        self.announce(msg)
        try:
            from app.ui.notifications import notify

            notify(msg, type="error")
        except Exception:
            pass

        self.push_screen(
            RollbackErrorModal(title="Rollback Failed", error_message=str(err))
        )
