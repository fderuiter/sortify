"""Integration and unit tests for unified partial undo state, CLI selective undo, and journaled ledger retry."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from textual.widgets import Button, DataTable
from textual.worker import WorkerCancelled

from app.config import AppSettings
from app.core.db_conn import clear_connection_cache, get_db_connection
from app.core.moves import VERIFIED
from app.core.pipeline import PipelineLifecycleState, SortPipeline
from app.core.session import AppSession
from app.core.session_catalog import (
    UNFINISHED_STATUSES,
    find_abandoned_sessions,
    get_session_moves,
    list_sessions,
)
from app.main import build_parser, handle_undo_command
from app.ui.modals.reports import AuditReportModal
from app.ui.modals.session import RollbackConfirmModal, SessionRecoveryModal
from app.ui.tui import AutoSorterTUI


@pytest.fixture
def isolated_app_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Isolate AUTOSORTER_APP_DIR and SORTIFY_KEYS_DIR per test."""
    app_dir = tmp_path / "app_store"
    keys_dir = tmp_path / "keys_store"
    app_dir.mkdir(parents=True, exist_ok=True)
    keys_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    monkeypatch.setenv("SORTIFY_KEYS_DIR", str(keys_dir))
    clear_connection_cache()
    yield tmp_path
    from app.core import services

    for graph in list(services._services.values()):
        graph.worker.stop(stop_embeddings=False)
    services._services.clear()
    clear_connection_cache()


async def _drain_workers(app: AutoSorterTUI, pilot: Any) -> None:
    """Wait for all active Textual workers to settle, ignoring exclusive-group cancellations."""
    for _ in range(6):
        await pilot.pause()
        try:
            await app.workers.wait_for_complete()
        except WorkerCancelled:
            continue
        await pilot.pause()
        if not any(w.is_running for w in app.workers):
            break


def _make_two_file_plan(base_dir: Path) -> tuple[Path, Path, dict]:
    """Create two files under base_dir and return (file_a, file_b, executable_plan)."""
    file_a = base_dir / "invoice_a.txt"
    file_b = base_dir / "notes_b.txt"
    file_a.write_text("Alpha invoice payload 2026", encoding="utf-8")
    file_b.write_text("Beta meeting notes payload 2026", encoding="utf-8")

    plan = {
        "Finance": {
            "invoice_a.txt": {
                "__type__": "file",
                "source_path": str(file_a),
                "relative_source": "invoice_a.txt",
            }
        },
        "Notes": {
            "notes_b.txt": {
                "__type__": "file",
                "source_path": str(file_b),
                "relative_source": "notes_b.txt",
            }
        },
    }
    return file_a, file_b, plan


def test_selective_undo_settled_on_restart_and_final_move_rolls_back(
    isolated_app_store: Path,
):
    """Criteria 1 & 3: Selective undo leaves zero abandoned sessions on restart and transitions to rolled_back when zero active moves remain."""
    assert "partially_rolled_back" not in UNFINISHED_STATUSES

    workspace = isolated_app_store / "workspace"
    workspace.mkdir()
    file_a, file_b, plan = _make_two_file_plan(workspace)

    settings = AppSettings()
    session = AppSession(settings, str(workspace))
    try:
        pipeline = SortPipeline(session)
        pipeline.transition_to(PipelineLifecycleState.PLANNED)
        exec_res = pipeline.execute(plan)
        assert exec_res.ok
        session_id = exec_res.session_id
        assert session_id

        dst_a = workspace / "Finance" / "invoice_a.txt"
        dst_b = workspace / "Notes" / "notes_b.txt"
        assert dst_a.exists() and dst_b.exists()

        moves = get_session_moves(session_id)
        assert len(moves) == 2
        step_a = next(
            m["step_id"]
            for m in moves
            if os.path.basename(m["source_path"]) == "invoice_a.txt"
        )
        step_b = next(
            m["step_id"]
            for m in moves
            if os.path.basename(m["source_path"]) == "notes_b.txt"
        )

        # Selectively undo only invoice_a.txt
        undo_one = pipeline.undo(session_id, selected_step_ids=[step_a])
        assert undo_one.ok
        assert file_a.exists()
        assert not dst_a.exists()
        assert dst_b.exists()
    finally:
        session.close()

    # Simulate restart: zero abandoned sessions when no .branches files are trapped
    assert find_abandoned_sessions() == []

    sessions_after_first = list_sessions()
    sess_entry = next(s for s in sessions_after_first if s["session_id"] == session_id)
    assert sess_entry["status"] == "partially_rolled_back"

    # Remaining moves only include step_b
    remaining = get_session_moves(session_id)
    assert [m["step_id"] for m in remaining] == [step_b]

    # Revert the final remaining move via full undo (selected_step_ids=None, matching Ctrl+Z)
    undo_rest = SortPipeline(None, settings=settings).undo(session_id)
    assert undo_rest.ok
    assert file_b.exists()
    assert not (workspace / "Notes" / "notes_b.txt").exists()

    sessions_after_second = list_sessions()
    sess_final = next(s for s in sessions_after_second if s["session_id"] == session_id)
    assert sess_final["status"] == "rolled_back"

    conn = get_db_connection(sess_final["history_db_path"])
    with conn:
        rem_cnt = conn.execute(
            "SELECT COUNT(*) FROM moves WHERE session_id = ? AND state IN ('done', 'verified')",
            (session_id,),
        ).fetchone()[0]
    assert rem_cnt == 0


def test_tui_ctrl_z_after_selective_undo_opens_rollback_modal_without_crash_prompt(
    isolated_app_store: Path,
):
    """Criteria 1, 2 & 3: Restarting TUI after selective undo shows no SessionRecoveryModal, and Ctrl+Z opens RollbackConfirmModal for remaining moves."""
    workspace = isolated_app_store / "tui_workspace"
    workspace.mkdir()
    file_a, file_b, plan = _make_two_file_plan(workspace)

    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    session = AppSession(settings, str(workspace))
    try:
        pipeline = SortPipeline(session)
        pipeline.transition_to(PipelineLifecycleState.PLANNED)
        res = pipeline.execute(plan)
        session_id = res.session_id
        assert session_id
        moves = get_session_moves(session_id)
        step_a = next(
            m["step_id"]
            for m in moves
            if os.path.basename(m["source_path"]) == "invoice_a.txt"
        )
        pipeline.undo(session_id, selected_step_ids=[step_a])
    finally:
        session.close()

    async def _run_tui_check():
        app = AutoSorterTUI(base_dir=str(workspace), settings=settings)
        async with app.run_test() as pilot:
            await pilot.pause()
            # Must NOT open SessionRecoveryModal on startup after selective undo
            assert not any(
                isinstance(scr, SessionRecoveryModal) for scr in app.screen_stack
            )

            # Wait for initial startup scan worker if active
            await app.workers.wait_for_complete()
            await pilot.pause()

            # Trigger Ctrl+Z (action_undo_last_sort)
            app.action_undo_last_sort()
            await pilot.pause()

            assert isinstance(app.screen, RollbackConfirmModal)
            modal: RollbackConfirmModal = app.screen
            assert modal.session_info["session_id"] == session_id
            assert len(modal.all_items) == 1
            assert os.path.basename(modal.all_items[0]["source_path"]) == "notes_b.txt"

            # Confirm rollback of remaining move (note _on_undo_success triggers _start_scan)
            modal.action_confirm()
            await _drain_workers(app, pilot)

            assert file_a.exists()
            assert file_b.exists()
            sess_final = next(
                s for s in list_sessions() if s["session_id"] == session_id
            )
            assert sess_final["status"] == "rolled_back"

    asyncio.run(_run_tui_check())


def test_cli_undo_list_moves_dry_run_step_id_and_file_text_and_json(
    isolated_app_store: Path, capsys: pytest.CaptureFixture[str]
):
    """Criterion 4: CLI undo supports --list-moves, --dry-run, --step-id, and --file in both text and --json modes."""
    workspace = isolated_app_store / "cli_workspace"
    workspace.mkdir()
    file_a, file_b, plan = _make_two_file_plan(workspace)

    settings = AppSettings()
    session = AppSession(settings, str(workspace))
    try:
        pipeline = SortPipeline(session)
        pipeline.transition_to(PipelineLifecycleState.PLANNED)
        res = pipeline.execute(plan)
        session_id = res.session_id
        assert session_id
    finally:
        session.close()

    parser = build_parser()

    def _invoke(cli_args: list[str]) -> tuple[int, str, str]:
        parsed = parser.parse_args(cli_args)
        with pytest.raises(SystemExit) as exc_info:
            handle_undo_command(parsed, AppSettings())
        captured = capsys.readouterr()
        code = int(exc_info.value.code or 0)
        return code, captured.out, captured.err

    # 1. --list-moves in JSON and text modes
    code, out, _ = _invoke(["undo", "--list-moves", "--json"])
    assert code == 0
    listed_json = json.loads(out)
    assert listed_json["status"] == "success"
    assert listed_json["session_id"] == session_id
    assert listed_json["count"] == 2
    step_map = {
        os.path.basename(m["source_path"]): m["step_id"] for m in listed_json["moves"]
    }
    assert set(step_map.keys()) == {"invoice_a.txt", "notes_b.txt"}

    code, out_txt, err_txt = _invoke(["undo", "--list-moves"])
    assert code == 0
    assert "invoice_a.txt" in out_txt
    assert "notes_b.txt" in out_txt
    assert session_id in err_txt

    # 2. --dry-run detects collision without modifying files on disk
    file_a.write_text("New conflicting file at original source!", encoding="utf-8")
    code, out, _ = _invoke(["undo", "--dry-run", "--file", "invoice_a.txt", "--json"])
    assert code == 0
    preview_json = json.loads(out)
    assert preview_json["status"] == "success"
    assert preview_json["dry_run"] is True
    assert preview_json["total_operations"] == 1
    assert preview_json["has_collisions"] is True
    assert preview_json["summary"]["collision"] == 1
    # Verify disk untouched by --dry-run
    assert (workspace / "Finance" / "invoice_a.txt").exists()
    file_a.unlink()

    code, out_txt, _ = _invoke(
        ["undo", "--dry-run", "--step-id", str(step_map["notes_b.txt"])]
    )
    assert code == 0
    assert "Clean: 1" in out_txt
    assert (workspace / "Notes" / "notes_b.txt").exists()

    # 3. Revert single file using --file in JSON mode
    code, out, _ = _invoke(["undo", "--file", "Finance/invoice_a.txt", "--json"])
    assert code == 0
    undo_a_json = json.loads(out)
    assert undo_a_json["status"] == "success"
    assert undo_a_json["restored"] == 1
    assert file_a.exists()
    assert not (workspace / "Finance" / "invoice_a.txt").exists()
    assert (workspace / "Notes" / "notes_b.txt").exists()

    # Session is now partially_rolled_back; --latest must still target it!
    sess_mid = next(s for s in list_sessions() if s["session_id"] == session_id)
    assert sess_mid["status"] == "partially_rolled_back"

    # 4. Revert remaining move using --step-id in text mode (defaults to --latest partially_rolled_back session)
    code, _, err_txt = _invoke(["undo", "--step-id", str(step_map["notes_b.txt"])])
    assert code == 0
    assert "Rollback completed successfully" in err_txt
    assert file_b.exists()
    assert not (workspace / "Notes" / "notes_b.txt").exists()

    sess_done = next(s for s in list_sessions() if s["session_id"] == session_id)
    assert sess_done["status"] == "rolled_back"

    # 5. Unknown --file or --step-id on explicit --session-id returns structured JSON error
    err_code, err_out, _ = _invoke(
        ["undo", "--session-id", session_id, "--file", "nonexistent.pdf", "--json"]
    )
    assert err_code == 1
    err_payload = json.loads(err_out)
    assert err_payload["status"] == "error"
    assert "nonexistent.pdf" in err_payload["message"]


def test_single_file_partial_commit_failure_transitions_failed_to_completed_on_retry(
    isolated_app_store: Path,
):
    """Verify a 1-file partial-commit sort where 0 moves initially succeed marks session failed and transitions to completed on retry."""
    workspace = isolated_app_store / "single_fail_ws"
    workspace.mkdir()
    src = workspace / "locked_report.txt"
    src.write_text("Quarterly compliance report", encoding="utf-8")
    plan = {
        "Reports": {
            "locked_report.txt": {
                "__type__": "file",
                "source_path": str(src),
                "relative_source": "locked_report.txt",
            }
        }
    }

    settings = AppSettings()
    settings._settings_model.MOVE_ERROR_POLICY = "partial_commit"
    assert settings.MOVE_ERROR_POLICY == "partial_commit"

    session = AppSession(settings, str(workspace))
    try:
        pipeline = SortPipeline(session)
        pipeline.transition_to(PipelineLifecycleState.PLANNED)

        with patch(
            "app.core.resilient_file_ops.resilient_move",
            side_effect=PermissionError("Locked"),
        ):
            res = pipeline.execute(plan)

        assert res.state == PipelineLifecycleState.FAILED
        session_id = res.session_id
        assert session_id
        audit = res.summary["audit_report"]
        assert audit["session_id"] == session_id
        assert len(audit["failed_items"]) == 1

        sess_failed = next(s for s in list_sessions() if s["session_id"] == session_id)
        assert sess_failed["status"] == "failed"

        # Now retry via AppSession after lock clears
        retry_res = session.retry_failed_moves(
            audit["failed_items"], session_id=session_id
        )
        assert retry_res["session_id"] == session_id
        assert retry_res["audit_report"]["verified_pass_count"] == 1

        sess_completed = next(
            s for s in list_sessions() if s["session_id"] == session_id
        )
        assert sess_completed["status"] == "completed"

        # Undo restores the retried file back to original source
        undo_res = pipeline.undo(session_id)
        assert undo_res.ok
        assert src.exists()
        assert not (workspace / "Reports" / "locked_report.txt").exists()
    finally:
        session.close()


def test_interactive_partial_commit_opens_audit_modal_and_journals_retry_for_undo(
    isolated_app_store: Path,
):
    """Criteria 5 & 6: Interactive single-file failure preserves verified moves, opens AuditReportModal, journals retry in moves as verified, and supports full undo."""
    workspace = isolated_app_store / "retry_workspace"
    workspace.mkdir()
    file_a, file_b, plan = _make_two_file_plan(workspace)

    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    from app.core.resilient_file_ops import resilient_move as real_resilient_move

    fail_b_once = {"locked": True}

    def flaky_resilient_move(src: str, dst: str) -> str:
        if fail_b_once["locked"] and os.path.basename(src) == "notes_b.txt":
            raise PermissionError("Simulated transient file lock on notes_b.txt")
        return real_resilient_move(src, dst)

    async def _run_interactive_partial_commit_and_retry():
        app = AutoSorterTUI(base_dir=str(workspace), settings=settings)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()

            # Ensure AppSession and executable plan are ready
            if app.app_session is None:
                app.app_session = AppSession(settings, str(workspace))
            app.plan = plan
            app.pipeline = SortPipeline(app.app_session)
            app.pipeline.transition_to(PipelineLifecycleState.PLANNED)

            with patch(
                "app.core.resilient_file_ops.resilient_move",
                side_effect=flaky_resilient_move,
            ):
                worker = app.run_execute_worker()
                try:
                    await worker.wait()
                except Exception:
                    pass
                await _drain_workers(app, pilot)

            # Verified move for file_a MUST be preserved, while file_b stayed in source
            dst_a = workspace / "Finance" / "invoice_a.txt"
            dst_b = workspace / "Notes" / "notes_b.txt"
            assert dst_a.exists()
            assert not file_a.exists()
            assert file_b.exists()
            assert not dst_b.exists()

            # Criterion 5: AuditReportModal opened automatically with failed item ready for retry
            assert isinstance(app.screen, AuditReportModal)
            modal: AuditReportModal = app.screen
            session_id = modal.report_data.get("session_id")
            assert session_id
            assert modal.report_data["verified_pass_count"] == 1
            assert modal.report_data["mismatch_fail_count"] == 1
            failed_table = modal.query_one("#failed-items-table", DataTable)
            assert failed_table.row_count == 1

            # Also verify ADR 0005 no-replace guarantee on retry: place an unrelated bystander file at dst_b
            dst_b.parent.mkdir(parents=True, exist_ok=True)
            bystander_text = "Unrelated bystander file that must never be overwritten"
            dst_b.write_text(bystander_text, encoding="utf-8")

            # Unlock file_b and press Retry All in AuditReportModal
            fail_b_once["locked"] = False
            btn_retry_all = modal.query_one("#btn-retry-all", Button)
            btn_retry_all.press()
            await _drain_workers(app, pilot)

            # Bystander preserved (ADR 0005), retried file moved to suffixed path, modal shows 2 passed / 0 failed
            assert dst_b.read_text(encoding="utf-8") == bystander_text
            suffixed_b = workspace / "Notes" / "notes_b_1.txt"
            assert suffixed_b.exists()
            assert (
                suffixed_b.read_text(encoding="utf-8")
                == "Beta meeting notes payload 2026"
            )
            assert not file_b.exists()
            assert modal.report_data["verified_pass_count"] == 2
            assert modal.report_data["mismatch_fail_count"] == 0

            # Criterion 6: Verify MoveLedger recorded a 'verified' row for the retried file!
            from app.core.history import open_session_history

            with open_session_history() as history_mgr:
                raw_moves = history_mgr.moves.list_moves(session_id)
            verified_rows = [m for m in raw_moves if m.state == VERIFIED]
            assert len(verified_rows) == 2
            assert all(m.size is not None and m.size > 0 for m in verified_rows)
            verified_dsts = {os.path.basename(m.dst) for m in verified_rows}
            assert verified_dsts == {"invoice_a.txt", "notes_b_1.txt"}

            # Dismiss AuditReportModal and undo the entire session via SortPipeline.undo
            modal.action_cancel()
            await pilot.pause()

            undo_res = SortPipeline(None, settings=settings).undo(session_id)
            assert undo_res.ok
            assert file_a.exists()
            assert file_b.exists()
            assert not dst_a.exists()
            assert not suffixed_b.exists()
            # Bystander remains intact
            assert dst_b.read_text(encoding="utf-8") == bystander_text

    asyncio.run(_run_interactive_partial_commit_and_retry())


def test_pipeline_execute_filters_extra_kwargs_for_fixed_signature_execute_moves():
    """Ensure SortPipeline.execute drops extra kwargs like error_policy when session.execute_moves has no **kwargs."""
    from unittest.mock import MagicMock

    called_with: dict[str, object] = {}

    def fixed_execute_moves(plan, progress_callback=None):
        called_with["plan"] = plan
        called_with["progress_callback"] = progress_callback
        return {"session_id": "sess-fixed", "moved": 1}

    mock_session = MagicMock()
    mock_session.base_dir = "."
    mock_session.settings = AppSettings()
    mock_session.execute_moves.side_effect = fixed_execute_moves

    pipeline = SortPipeline(mock_session)
    pipeline.transition_to(PipelineLifecycleState.PLANNED)
    res = pipeline.execute(
        {"Docs": {}},
        progress_callback=lambda *a, **kw: None,
        error_policy="partial_commit",
    )
    assert res.ok
    assert res.session_id == "sess-fixed"
    assert called_with["plan"] == {"Docs": {}}
    assert callable(called_with["progress_callback"])
