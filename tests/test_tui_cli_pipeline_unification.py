"""Regression tests for unified TUI and CLI workflows on SortPipeline (ADR 0007)."""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from textual.widgets import Button

from app.config import AppSettings
from app.core.audit_reporter import resolve_audit_report
from app.core.metadata import MetadataPass, evaluate_static_file_routing
from app.core.pipeline import (
    PipelineLifecycleState,
    SortPipeline,
    apply_drop_reroot,
    group_drop_targets,
    restore_rerooted_source_dir,
    validate_drop_destination,
)
from app.core.services import acquire_services
from app.core.session import AppSession
from app.core.store import open_store
from app.ui.modals.drop_zone import DropZoneModal
from app.ui.modals.session import (
    HistoryModal,
    MultiSessionRecoveryModal,
    RollbackConfirmModal,
)
from app.ui.tui import AutoSorterTUI
from scripts.validate_architecture import ArchitectureASTVisitor


def _configure_test_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AppSettings:
    """Configure isolated app store and keyring-free test key environment."""
    app_dir = tmp_path / "app_store"
    app_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    monkeypatch.setenv("SORTIFY_DB_KEY", "a" * 64)
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    return settings


def test_import_layers_forbid_history_imports_in_ui() -> None:
    """Verify validate_architecture rejects direct app.core.history imports in app.ui."""
    for snippet in (
        "from app.core.history import open_session_history\n",
        "import app.core.history\n",
        "def f():\n    from app.core.history import HistoryManager\n",
    ):
        errors: list[str] = []
        ArchitectureASTVisitor("app/ui/modals/session.py", errors).visit(
            ast.parse(snippet)
        )
        assert errors, f"Expected layer violation for snippet: {snippet!r}"
        assert "'app.ui.modals.session'" in errors[0]
        assert "'app.core.history'" in errors[0]


@pytest.mark.anyio
async def test_tui_scan_worker_delegates_to_sort_pipeline_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify run_scan_worker uses SortPipeline.plan for multi-root, custom quarantine, MAX_WORKERS validation, and telemetry."""
    settings = _configure_test_env(monkeypatch, tmp_path)
    settings._settings_model.QUARANTINE_DIR_NAME = "_Custom_Quarantine_Vault"

    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    quarantine_root = tmp_path / "_Custom_Quarantine_Vault"
    for d in (root_a, root_b, quarantine_root):
        d.mkdir()
    (root_a / "invoice_q1.txt").write_text("Quarterly invoice summary alpha")
    (root_a / "_Custom_Quarantine_Vault").mkdir()
    (root_a / "_Custom_Quarantine_Vault" / "secret.txt").write_text("quarantined doc")
    (root_b / "invoice_q2.txt").write_text("Quarterly invoice summary beta")
    (quarantine_root / "isolated.txt").write_text("root quarantine doc")

    # Force invalid bool MAX_WORKERS on runtime settings instance to test fallback to 2
    object.__setattr__(settings, "MAX_WORKERS", True)

    app = AutoSorterTUI(settings=settings, base_dir=str(root_a))
    app.source_dirs = [str(root_a), str(root_b), str(quarantine_root)]

    telemetry_events: list[str] = []
    progress_stages: list[str] = []
    observed_workers: list[object] = []

    orig_handle_telemetry = app._handle_pipeline_telemetry
    orig_handle_progress = app._handle_progress_update

    def spy_telemetry(event_name: str, payload: dict) -> None:
        telemetry_events.append(event_name)
        orig_handle_telemetry(event_name, payload)

    def spy_progress(update_or_prog, stage=None, **kwargs) -> None:
        if stage:
            progress_stages.append(str(stage))
        elif hasattr(update_or_prog, "stage") and update_or_prog.stage:
            progress_stages.append(str(update_or_prog.stage))
        orig_handle_progress(update_or_prog, stage, **kwargs)

    from app.core import extractor as extractor_mod

    orig_corpus_gen = extractor_mod.build_corpus_generator

    def spy_corpus_gen(*args, **kwargs):
        observed_workers.append(kwargs.get("max_workers"))
        return orig_corpus_gen(*args, **kwargs)

    monkeypatch.setattr(app, "_handle_pipeline_telemetry", spy_telemetry)
    monkeypatch.setattr(app, "_handle_progress_update", spy_progress)
    monkeypatch.setattr(extractor_mod, "build_corpus_generator", spy_corpus_gen)
    monkeypatch.setattr(
        "app.core.session_catalog.find_abandoned_sessions", lambda *a, **k: []
    )

    try:
        async with app.run_test() as pilot:
            worker = app.run_scan_worker()
            await worker.wait()
            await pilot.pause()

            assert app.pipeline is not None
            assert app.pipeline.state == PipelineLifecycleState.PLANNED
            assert "plan_start" in telemetry_events
            assert "plan_finish" in telemetry_events
            assert observed_workers == [2]
            assert not isinstance(observed_workers[0], bool)

            planned = app.pipeline.planned_items
            assert len(planned) == 2
            assert all("_Custom_Quarantine_Vault" not in item for item in planned)
            assert any("Extracting text (" in stage for stage in progress_stages)
    finally:
        if app.app_session:
            app.app_session.close()


def test_dropzone_is_supported_file_matches_policy_engine_and_metadata_pass(
    tmp_path: Path,
) -> None:
    """Verify DropZoneModal._is_supported_file delegates halting and action policies to PolicyEngine."""

    class PolicySettings:
        IGNORED_EXTENSIONS = [".tmp"]
        EXTENSION_CATEGORIES = {".dat": "Data"}
        KEYWORD_RULES = {"fallbackkw": "FallbackFolder"}
        LEARNED_RULES = {}
        POLICIES = []

    settings = PolicySettings()
    modal = DropZoneModal(settings=settings, base_dir=str(tmp_path))

    # 1. ExtractorRegistry formats remain supported regardless of static rules
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_text("pdf")
    assert modal._is_supported_file(str(pdf_path)) is True

    # 2. Action-only policy (no target_path) makes unknown extension supported
    action_file = tmp_path / "audit_packet.unknownext"
    action_file.write_text("binary")
    settings.POLICIES = [
        {
            "type": "action",
            "expression": "audit_packet",
            "action": "archive",
            "priority": 50,
            "enabled": True,
        }
    ]
    assert modal._is_supported_file(str(action_file)) is True

    # 3. Non-matching halting policy stops evaluation before KEYWORD_RULES and EXTENSION_CATEGORIES,
    # matching MetadataPass.run behavior.
    halted_dat = tmp_path / "fallbackkw_item.dat"
    halted_dat.write_text("payload")
    halted_txt = tmp_path / "fallbackkw_item.txt"
    halted_txt.write_text("text payload")
    settings.POLICIES = [
        {
            "type": "pattern",
            "expression": "*.must_match_first",
            "target_path": "Strict",
            "priority": 100,
            "halting": True,
            "enabled": True,
        }
    ]
    target, has_action, halted = evaluate_static_file_routing(str(halted_dat), settings)
    assert halted is True
    assert target is None
    assert has_action is False
    assert modal._is_supported_file(str(halted_dat)) is False

    upserted: list[list[tuple]] = []

    class DummyDB:
        def get_all_documents(self, *args, **kwargs):
            return []

        def get_all_document_ratings(self, *args, **kwargs):
            return {}

        def upsert_documents(self, docs):
            upserted.append(list(docs))

    assert (
        MetadataPass.run(str(tmp_path), ["fallbackkw_item.txt"], settings, DummyDB())
        == []
    )
    MetadataPass.run(str(tmp_path), ["fallbackkw_item.dat"], settings, DummyDB())
    assert upserted[-1][0][3] == "[STATUS:UNSUPPORTED]"


def test_shared_drop_helpers_validation_grouping_and_reroot_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify shared destination validation, drop_scope grouping, reroot_plan, and source dir restoration."""
    settings = _configure_test_env(monkeypatch, tmp_path)
    protected_dir = tmp_path / "protected_vault"
    protected_dir.mkdir()
    not_a_dir = tmp_path / "plain_file.txt"
    not_a_dir.write_text("file")
    settings._settings_model.PROTECTED_PATHS = [str(protected_dir)]

    _, err_prot = validate_drop_destination(str(protected_dir), settings=settings)
    assert err_prot == "protected"

    _, err_file = validate_drop_destination(str(not_a_dir), settings=settings)
    assert err_file == "not_directory"

    dest_dir = tmp_path / "sorted_dest"
    resolved_dest, err_ok = validate_drop_destination(str(dest_dir), settings=settings)
    assert err_ok is None
    assert resolved_dest == str(dest_dir.resolve())

    incoming_dir = tmp_path / "incoming_batch"
    nested = incoming_dir / "sub"
    nested.mkdir(parents=True)
    (nested / "invoice_1.txt").write_text("Invoice billing statement 100")

    groups = group_drop_targets([str(incoming_dir)], settings=settings)
    assert list(groups.keys()) == [str(incoming_dir)]
    assert groups[str(incoming_dir)] == [os.path.join("sub", "invoice_1.txt")]

    session = AppSession(settings, base_dir=str(incoming_dir))
    try:
        pipeline = SortPipeline(session)
        plan = pipeline.plan(groups[str(incoming_dir)], dest_dir=resolved_dest)
        plan, re_rooted = apply_drop_reroot(
            session, plan, str(incoming_dir), resolved_dest
        )
        assert re_rooted is True
        assert session.base_dir == resolved_dest
        result = pipeline.execute(plan)
        assert result.ok
        restore_rerooted_source_dir(
            str(incoming_dir), was_dir=True, re_rooted=re_rooted
        )
        assert incoming_dir.is_dir()
    finally:
        session.close()


@pytest.mark.anyio
async def test_tui_and_history_undo_workers_use_sort_pipeline_without_orphaned_threads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify AutoSorterTUI.run_undo_worker, HistoryModal.run_rollback_worker, and CLI undo use SortPipeline.undo and close ServiceLeases cleanly."""
    settings = _configure_test_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "app.core.session_catalog.find_abandoned_sessions", lambda *a, **k: []
    )

    def _create_completed_move_session(folder_name: str) -> tuple[str, Path, Path]:
        work_dir = tmp_path / folder_name
        sorted_dir = work_dir / "Invoices"
        sorted_dir.mkdir(parents=True)
        orig_file = work_dir / "doc.txt"
        moved_file = sorted_dir / "doc.txt"
        moved_file.write_text("restorable content")

        file_sha = hashlib.sha256(moved_file.read_bytes()).hexdigest()
        store = open_store()
        with acquire_services(store) as svc:
            sid = svc.history.start_session(str(work_dir))
            seq = svc.history.moves.record_intent(
                sid,
                str(orig_file),
                str(moved_file),
                src_sha256=file_sha,
                size=moved_file.stat().st_size,
            )
            svc.history.moves.mark_done(sid, seq, str(moved_file))
            svc.history.complete_session(sid)
        return sid, orig_file, moved_file

    def _db_worker_threads() -> list[threading.Thread]:
        return [
            t for t in threading.enumerate() if t.is_alive() and "DBWorker" in t.name
        ]

    baseline_threads = set(_db_worker_threads())

    # 1. AutoSorterTUI.run_undo_worker (with app_session=None)
    sid1, orig1, moved1 = _create_completed_move_session("ws_tui_undo")
    states_seen: list[PipelineLifecycleState] = []
    app = AutoSorterTUI(settings=settings)
    orig_transition = app._handle_pipeline_state_transition

    def record_transition(old_s, new_s, ctx=None):
        states_seen.append(new_s)
        orig_transition(old_s, new_s, ctx)

    monkeypatch.setattr(app, "_handle_pipeline_state_transition", record_transition)

    async with app.run_test() as pilot:
        worker = app.run_undo_worker(sid1, ignore_missing=False)
        await worker.wait()
        await pilot.pause()

        assert PipelineLifecycleState.ROLLING_BACK in states_seen
        assert PipelineLifecycleState.ROLLED_BACK in states_seen
        assert orig1.exists()
        assert not moved1.exists()

    assert set(_db_worker_threads()) <= baseline_threads

    # 2. HistoryModal.run_rollback_worker + Escape blocked while ROLLING_BACK
    sid2, orig2, moved2 = _create_completed_move_session("ws_history_undo")
    entered_evt = threading.Event()
    release_evt = threading.Event()
    orig_undo = SortPipeline.undo

    def blocking_pipeline_undo(self_pipe, session_id, **kwargs):
        # Enter ROLLING_BACK then pause inside history.rollback via monkeypatch
        from app.core.history import HistoryManager

        real_rb = HistoryManager.rollback

        def _slow_rb(hm_self, s_id, **rb_kw):
            entered_evt.set()
            release_evt.wait(timeout=5.0)
            return real_rb(hm_self, s_id, **rb_kw)

        with patch.object(HistoryManager, "rollback", _slow_rb):
            return orig_undo(self_pipe, session_id, **kwargs)

    app2 = AutoSorterTUI(settings=settings)
    async with app2.run_test() as pilot:
        modal = HistoryModal()
        app2.push_screen(modal)
        await pilot.pause()

        with patch.object(SortPipeline, "undo", blocking_pipeline_undo):
            modal._rollback_in_progress = True
            rb_worker = modal.run_rollback_worker(sid2, False)
            await pilot.pause()
            assert await asyncio.to_thread(entered_evt.wait, 5.0)
            assert (
                modal._rollback_pipeline is not None
                and modal._rollback_pipeline.state
                == PipelineLifecycleState.ROLLING_BACK
            )

            await pilot.press("escape")
            await pilot.pause()
            assert app2.screen is modal

            release_evt.set()
            await rb_worker.wait()
            await pilot.pause()

        assert orig2.exists()
        assert not moved2.exists()

    assert set(_db_worker_threads()) <= baseline_threads

    # 3. CLI handle_undo_command routes through SortPipeline.undo
    from app.main import handle_undo_command

    sid3, orig3, moved3 = _create_completed_move_session("ws_cli_undo")
    cli_args = argparse.Namespace(
        session_id=sid3,
        latest=False,
        force=False,
        json=True,
        quiet=True,
    )
    with pytest.raises(SystemExit) as exc_info:
        handle_undo_command(cli_args, settings)
    assert exc_info.value.code == 0
    assert orig3.exists()
    assert not moved3.exists()
    assert set(_db_worker_threads()) <= baseline_threads


@pytest.mark.anyio
async def test_rollback_preview_sizes_recovery_failure_and_audit_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify dry-run size previews, MultiSessionRecoveryModal failed rollback handling, and resolve_audit_report."""
    settings = _configure_test_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "app.core.session_catalog.find_abandoned_sessions", lambda *a, **k: []
    )

    # 1. RollbackConfirmModal._compute_dryrun_preview populates file sizes from target on disk
    src_path = tmp_path / "orig.txt"
    dst_path = tmp_path / "Sorted" / "orig.txt"
    dst_path.parent.mkdir(parents=True)
    dst_path.write_bytes(b"x" * 128)

    session_info = {
        "session_id": "sess_in_memory_preview",
        "base_dir": str(tmp_path),
        "items": [
            {
                "step_id": 1,
                "source_path": str(src_path),
                "target_path": str(dst_path),
                "size": None,
                "status": "completed",
            }
        ],
    }

    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
    async with app.run_test() as pilot:
        confirm_modal = RollbackConfirmModal(session_info=session_info)
        app.push_screen(confirm_modal)
        await pilot.pause()

        preview = confirm_modal._compute_dryrun_preview()
        assert preview["total_operations"] == 1
        assert preview["steps"][0]["status"] == "CLEAN"
        assert preview["steps"][0]["size"] == 128

    # 2. MultiSessionRecoveryModal.action_rollback shares _do_recovery_rollback failure handling
    abandoned = [
        {
            "session_id": "sess_fail_01",
            "base_dir": str(tmp_path),
            "status": "failed",
            "step_count": 2,
        },
        {
            "session_id": "sess_ok_02",
            "base_dir": str(tmp_path),
            "status": "failed",
            "step_count": 1,
        },
    ]
    app_rec = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
    app_rec.action_scan_directory = lambda: None
    async with app_rec.run_test() as pilot:
        rec_modal = MultiSessionRecoveryModal(list(abandoned), settings=settings)
        app_rec.push_screen(rec_modal)
        await pilot.pause()

        with patch(
            "app.core.session_catalog.roll_back_abandoned_session", return_value=False
        ):
            rec_modal.query_one("#btn-rollback", Button).press()
            await pilot.pause()

            # Failed rollback keeps the session in the table and announces failure
            assert len(rec_modal.sessions) == 2
            modal_msgs = [a["message"] for a in rec_modal.announcements]
            assert any("recovery failed" in m.lower() for m in modal_msgs)

    # 3. resolve_audit_report synthesizes report from plan when summary has no audit_report
    plan = {"Docs": {"a.txt": {"__type__": "file", "relative_source": "a.txt"}}}
    synthesized = resolve_audit_report(None, str(tmp_path), plan)
    assert synthesized is not None
    assert synthesized["total_files"] == 1
    assert resolve_audit_report(synthesized, str(tmp_path), None) is synthesized
