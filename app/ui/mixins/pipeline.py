"""Scan, validation and execute workers that drive a sort from the TUI."""

import logging
import os
from typing import Any, Dict, Optional

from textual import work
from textual.widgets import Button, Static

from app.ui.modals.edit import (
    InteractiveCollisionResolverModal,
)

logger = logging.getLogger(__name__)

EXECUTE_NEEDS_SCAN_TOOLTIP = "Execution disabled: Scan a folder first [Ctrl+S]"


class SortPipelineMixin:
    """Run the scan, plan validation and move execution workers for the TUI."""

    def action_scan_directory(self) -> None:
        """Trigger directory scanning background worker."""
        if self._is_text_control_focused(allow_tree_search=True):
            return
        if not getattr(self, "source_dirs", None) and self.base_dir:
            self.source_dirs = [self.base_dir]
        if not getattr(self, "source_dirs", None) or not any(
            os.path.exists(d) for d in self.source_dirs
        ):
            self.action_select_dir()
            return
        if self._announce_if_busy("scan"):
            return
        self._start_scan()

    def _start_scan(self) -> None:
        """Announce and launch the scan worker without the busy guard."""
        self.announce(f"Scanning directories: {', '.join(self.source_dirs)} ...")
        self.run_scan_worker()

    def _running_pipeline_worker(self) -> Optional[str]:
        """Return the name of a running scan or execute worker, if any."""
        for worker in self.workers:
            if worker.is_running and worker.name in (
                "run_scan_worker",
                "run_execute_worker",
            ):
                return worker.name
        return None

    def _announce_if_busy(self, action: str) -> bool:
        """Refuse to start ``action`` while a scan or move is still running.

        Both workers share the default exclusive group, so starting one cancels
        the other and replaces the AppSession that a running move depends on.
        """
        running = self._running_pipeline_worker()
        if running is None:
            return False
        if running == "run_execute_worker":
            msg = f"Files are still being moved. Wait for it to finish before you {action}."
        else:
            msg = f"A scan is still running. Wait for it to finish before you {action}."
        self.announce(msg, priority="assertive")
        return True

    @work(exclusive=True, thread=True)
    def run_scan_worker(self) -> None:
        """Execute scan and plan generation in worker thread."""
        try:
            from app.core.session import AppSession

            if self.app_session:
                try:
                    self.app_session.close()
                except Exception:
                    pass

            self.app_session = AppSession(
                self.settings,
                self.base_dir,
                source_dirs=getattr(self, "source_dirs", None),
            )

            from app.core.scanner import get_files_from_sources

            s_dirs = getattr(self, "source_dirs", None) or (
                [self.base_dir] if self.base_dir else []
            )
            files = get_files_from_sources(s_dirs)

            from app.core.metadata import MetadataPass

            def scan_progress_cb(
                update_or_prog: Any, stage: Optional[str] = None, **kwargs: Any
            ) -> None:
                self.call_from_thread(
                    self._handle_progress_update, update_or_prog, stage, **kwargs
                )

            bypassed = MetadataPass.run(
                self.base_dir,
                files,
                self.settings,
                self.app_session.db,
                progress_callback=scan_progress_cb,
                cancel_check=lambda: False,
                source_dirs=s_dirs,
            )

            # Files no metadata rule matched still need their text extracted and
            # fitted, otherwise clustering sees no documents and the plan is empty.
            bypassed_set = {str(f).replace("\\", "/") for f in (bypassed or [])}
            items_to_extract = [
                f for f in files if str(f).replace("\\", "/") not in bypassed_set
            ]
            if items_to_extract:
                from app.core.extractor import build_corpus_generator

                total = len(items_to_extract)
                done = 0
                for chunk in build_corpus_generator(
                    base_dir=self.base_dir,
                    items_to_sort=items_to_extract,
                    progress_callback=lambda *args, **kwargs: None,
                    max_workers=getattr(self.settings, "MAX_WORKERS", 2),
                    db=self.app_session.db,
                    settings=self.settings,
                ):
                    self.app_session.partial_fit(chunk)
                    done += len(chunk)
                    scan_progress_cb(done / total, f"Extracting text ({done}/{total})")

            # Load locks & ratings
            try:
                docs = self.app_session.db.get_all_documents(
                    self.base_dir, source_dirs=s_dirs
                )
                for d in docs:
                    if len(d) > 3 and d[3]:
                        self.locked_files[d[0]] = d[3]
            except Exception:
                pass

            try:
                self._ratings_cache = self.app_session.db.get_all_document_ratings(
                    self.base_dir
                )
            except Exception:
                pass

            self.plan = self.app_session.generate_sorting_plan()
            self.call_from_thread(self.rebuild_tree)
            model_status = getattr(self.plan, "model_status", "HEALTHY")
            invalid_locks = getattr(self.plan, "invalid_locks", {}) or {}
            if not invalid_locks and isinstance(self.plan, dict):
                invalid_locks = self.plan.get("invalid_locks", {}) or {}
            num_invalid = len(invalid_locks)

            if model_status == "DEGRADED_FALLBACK" or (
                isinstance(self.plan, dict)
                and self.plan.get("model_status") == "DEGRADED_FALLBACK"
            ):
                reason = (
                    getattr(self.plan, "degradation_reason", None)
                    or self.plan.get("degradation_reason")
                    or "Model unavailable"
                )
                rec_action = (
                    getattr(self.plan, "recovery_action", None)
                    or self.plan.get("recovery_action")
                    or "Re-download model bundle"
                )
                msg = f"Scan complete (DEGRADED FALLBACK MODE: {reason}. Suggested action: {rec_action}). Analyzed {len(files)} files."
                self.call_from_thread(
                    self.notify,
                    f"Sorting plan generated in degraded fallback mode: {reason}",
                    title=f"Suggested recovery action: {rec_action}",
                    severity="warning",
                )
            else:
                msg = f"Scan complete. Analyzed {len(files)} files into proposed plan."

            if num_invalid > 0:
                lock_str = (
                    "1 bad lock path was skipped"
                    if num_invalid == 1
                    else f"{num_invalid} bad lock paths were skipped"
                )
                msg += f" ({lock_str})."
            self.call_from_thread(self.announce, msg)
        except Exception as e:
            logger.error(f"Error in run_scan_worker: {e}")
            self.call_from_thread(self.announce, f"Scan error: {e}")
        finally:
            self.call_from_thread(self.reset_progress_bar)

    def action_execute_sort(self) -> None:
        """Trigger sorting plan execution worker with pre-move duplicate cluster review."""
        if self._is_text_control_focused(allow_tree_search=True):
            return
        if not self.plan or not self.app_session:
            self.announce("No plan available to execute. Run [Ctrl+S] Scan first.")
            return
        if self._announce_if_busy("execute the plan"):
            return

        exec_plan = self._get_executable_plan()
        if not self._has_file_nodes(exec_plan):
            self.announce("No checked files in sorting plan to execute.")
            return

        if getattr(self, "has_critical_errors", False):
            self.announce(
                "Cannot execute plan: Critical validation errors exist (see Alert Banner)."
            )
            return

        base_dir = self.app_session.base_dir if self.app_session else "."
        from app.core.verifier import VerificationEngine

        duplicate_groups = VerificationEngine.get_duplicate_groups(base_dir, exec_plan)
        if duplicate_groups:

            def handle_duplicate_modal_result(
                selections: Optional[Dict[str, str]],
            ) -> None:
                if selections is None:
                    self.announce("Move execution cancelled during duplicate review.")
                    return

                VerificationEngine.apply_deduplication_selections(
                    base_dir, self.plan, selections
                )
                self.announce(
                    "Executing file moves according to plan with deduplication applied..."
                )
                self.run_execute_worker()

            self.push_screen(
                InteractiveCollisionResolverModal(duplicate_groups=duplicate_groups),
                handle_duplicate_modal_result,
            )
        else:
            self.announce("Executing file moves according to plan...")
            self.run_execute_worker()

    @work(group="validation", exclusive=True, thread=True)
    def run_validation_worker(self) -> None:
        """Asynchronously verify plan integrity in background worker thread."""
        try:
            from app.core.verifier import VerificationEngine

            exec_plan = self._get_executable_plan()
            if not self.plan or not self._has_file_nodes(exec_plan):
                self.call_from_thread(self._update_validation_state, {})
                return

            base_dir = (
                self.app_session.base_dir
                if self.app_session
                else (self.base_dir or ".")
            )
            result = VerificationEngine.verify_plan_integrity(base_dir, exec_plan)
            self.call_from_thread(self._update_validation_state, result)
        except Exception as e:
            logger.error(f"Error running validation worker: {e}")

    def _update_validation_state(self, result: Dict[str, Any]) -> None:
        """Update UI controls, alert banner, and tree highlights based on validation result."""
        self.verification_result = result or {}

        collisions = result.get("collisions", [])
        circular = result.get("circular_renames", [])
        invalid = result.get("invalid_renames", [])
        long_paths = result.get("long_paths", [])

        broken_links = result.get("broken_links", [])
        unconfirmed = result.get("unconfirmed_renames", [])

        structural_collisions = [
            c
            for c in collisions
            if c.get("type") not in ("duplicate_target", "target_collision")
        ]
        resolvable_collisions = [
            c
            for c in collisions
            if c.get("type") in ("duplicate_target", "target_collision")
        ]

        has_critical = bool(
            result.get("has_critical_errors")
            if "has_critical_errors" in result
            else (structural_collisions or circular or invalid or long_paths)
        )
        has_warnings = bool(broken_links or unconfirmed or resolvable_collisions)

        self.has_critical_errors = has_critical

        # Build path sets for tree node highlighting
        crit_paths = set()
        for c in structural_collisions:
            if c.get("path"):
                crit_paths.add(os.path.normpath(c["path"]))
            if c.get("source"):
                crit_paths.add(os.path.normpath(c["source"]))
            for s in c.get("sources", []):
                crit_paths.add(os.path.normpath(s))
        for r in circular:
            if r.get("path"):
                crit_paths.add(os.path.normpath(r["path"]))
            for p in r.get("cycle", []):
                crit_paths.add(os.path.normpath(p))
        for inv in invalid:
            if inv.get("path"):
                crit_paths.add(os.path.normpath(inv["path"]))
            if inv.get("source"):
                crit_paths.add(os.path.normpath(inv["source"]))
        for lp in long_paths:
            if lp.get("path"):
                crit_paths.add(os.path.normpath(lp["path"]))
            if lp.get("source"):
                crit_paths.add(os.path.normpath(lp["source"]))

        warn_paths = set()
        for c in resolvable_collisions:
            if c.get("path"):
                warn_paths.add(os.path.normpath(c["path"]))
            if c.get("source"):
                warn_paths.add(os.path.normpath(c["source"]))
            for s in c.get("sources", []):
                warn_paths.add(os.path.normpath(s))
        for bl in broken_links:
            if bl.get("path"):
                warn_paths.add(os.path.normpath(bl["path"]))
        for unc in unconfirmed:
            if unc.get("path"):
                warn_paths.add(os.path.normpath(unc["path"]))
            if unc.get("source"):
                warn_paths.add(os.path.normpath(unc["source"]))

        self.critical_error_paths = crit_paths
        self.warning_paths = warn_paths

        # Update Execute Button
        try:
            btn_exec = self.query_one("#btn-execute", Button)
            exec_plan = self._get_executable_plan()
            has_files = self._has_file_nodes(exec_plan)

            if has_critical or not has_files:
                btn_exec.disabled = True
                if has_critical:
                    btn_exec.tooltip = "Execution disabled: Plan contains blocking critical validation errors"
                elif not self.plan:
                    btn_exec.tooltip = EXECUTE_NEEDS_SCAN_TOOLTIP
                else:
                    btn_exec.tooltip = (
                        "Execution disabled: No files selected to execute"
                    )
            else:
                btn_exec.disabled = False
                if resolvable_collisions:
                    btn_exec.tooltip = (
                        "Execute plan with interactive collision resolution [Ctrl+E]"
                    )
                else:
                    btn_exec.tooltip = "Execute file sorting plan [Ctrl+E]"
        except Exception:
            pass

        # Update Alert Banner
        try:
            alert_banner = self.query_one("#alert-banner", Static)
            if has_critical:
                alert_banner.display = True
                alert_banner.remove_class("alert-warning")
                alert_banner.add_class("alert-critical")

                msgs = []
                if structural_collisions:
                    msgs.append("⛔ STRUCTURAL DIRECTORY COLLISION(S) DETECTED:")
                    for c in structural_collisions:
                        dst_p = c.get("path", "")
                        msgs.append(
                            f"  • Destination: {dst_p} ({c.get('message', 'Collision')})"
                        )
                if circular:
                    msgs.append("⛔ CIRCULAR RENAMING DEPENDENCY DETECTED:")
                    for r in circular:
                        msgs.append(f"  • {r.get('message')}")
                if invalid:
                    msgs.append("⛔ INVALID FILENAME / STRUCTURE:")
                    for inv in invalid:
                        msgs.append(f"  • {inv.get('message')}")
                if long_paths:
                    msgs.append("⛔ EXCEEDS PATH LENGTH LIMIT:")
                    for lp in long_paths:
                        msgs.append(f"  • {lp.get('message')}")

                alert_str = "\n".join(msgs)
                alert_banner.update(alert_str)
                self.announce(f"Alert Banner: {alert_str}")
            elif has_warnings:
                alert_banner.display = True
                alert_banner.remove_class("alert-critical")
                alert_banner.add_class("alert-warning")

                msgs = [
                    "⚠️ NON-FATAL WARNING(S) / RESOLVABLE COLLISION(S) REPORTED (Execution unblocked):"
                ]
                if resolvable_collisions:
                    msgs.append(
                        "  • Destination path collisions exist - click 'Execute' to review & resolve interactively."
                    )
                for bl in broken_links:
                    msgs.append(f"  • {bl.get('message')}")
                for unc in unconfirmed:
                    msgs.append(f"  • {unc.get('message')}")

                alert_str = "\n".join(msgs)
                alert_banner.update(alert_str)
            else:
                alert_banner.display = False
                alert_banner.update("")
        except Exception as e:
            logger.debug(f"Error updating alert banner: {e}")

        # Update node styling in tree view
        self._apply_tree_node_styles()

    @work(exclusive=True, thread=True)
    def run_execute_worker(self) -> None:
        """Execute moves in worker thread."""
        try:

            def move_progress_cb(
                update_or_prog: Any, stage: Optional[str] = None, **kwargs: Any
            ) -> None:
                self.call_from_thread(
                    self._handle_progress_update, update_or_prog, stage, **kwargs
                )

            exec_plan = self._get_executable_plan()
            summary = self.app_session.execute_moves(
                exec_plan,
                progress_callback=move_progress_cb,
            )
            msg = f"Execution completed successfully! Summary: {summary}"
            self.call_from_thread(self.announce, msg)

            audit_report = (
                summary.get("audit_report") if isinstance(summary, dict) else None
            )
            if not audit_report and exec_plan:
                from app.core.audit_reporter import generate_audit_report
                from app.core.verifier import VerificationEngine

                base_dir = self.app_session.base_dir if self.app_session else "."
                moves = VerificationEngine.get_moves(base_dir, exec_plan)
                records = [
                    {"source_path": src, "destination_path": dst}
                    for _, src, dst in moves
                ]
                audit_report = generate_audit_report(base_dir, records)

            if audit_report:
                self.call_from_thread(self.action_open_audit_report, audit_report)
            # Rescan last: the scan worker replaces the AppSession this worker uses.
            self.call_from_thread(self._start_scan)
        except Exception as e:
            logger.error(f"Error executing moves: {e}")
            self.call_from_thread(self.announce, f"Execution error: {e}")
        finally:
            self.call_from_thread(self.reset_progress_bar)
