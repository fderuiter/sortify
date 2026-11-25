import os
import shutil
from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings as Settings
from app.core.session import AppSession
from app.core.session_catalog import find_abandoned_sessions
from app.ui.tui import AutoSorterTUI

pytestmark = pytest.mark.xdist_group(name="tui")


@pytest.fixture
def mock_session_base(tmp_path, monkeypatch):
    """Isolate the app store and session base directory to tmp_path.

    These tests leave unfinished sessions behind, and a TUI started by a later
    test would offer to recover them from a shared store.
    """
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app"))
    session_base = tmp_path / "autosorter_sessions"
    session_base.mkdir(parents=True, exist_ok=True)

    def mock_get_session_base_dir():
        return session_base

    monkeypatch.setattr(
        "app.core.path_utils.get_session_base_dir", mock_get_session_base_dir
    )
    return session_base


def test_resilient_move_retries_transient_lock(tmp_path, monkeypatch):
    """Test that resilient_move retries and succeeds when transient locks occur."""
    from app.core.resilient_file_ops import resilient_move

    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("content")

    from app.core import resilient_file_ops

    original_atomic_move = resilient_file_ops.atomic_move_non_clobber

    attempts = [0]

    def mock_atomic_move(s, d):
        attempts[0] += 1
        if attempts[0] < 3:
            raise PermissionError("File in use by another process")
        return original_atomic_move(s, d)

    monkeypatch.setattr(
        "app.core.resilient_file_ops.atomic_move_non_clobber", mock_atomic_move
    )
    monkeypatch.setattr("app.core.resilient_file_ops.RETRY_DELAY", 0.01)

    resilient_move(str(src), str(dst))
    assert dst.exists()
    assert dst.read_text() == "content"
    assert attempts[0] == 3


def test_unrecoverable_error_preserves_safety_storage_and_session(
    tmp_path, mock_session_base
):
    """Test that if recovery rollback fails due to a permission/move error, safety storage and session directory are preserved."""
    base_dir = str(tmp_path / "work_dir")
    os.makedirs(base_dir, exist_ok=True)

    file_src = os.path.join(base_dir, "document.pdf")
    with open(file_src, "w") as f:
        f.write("original pdf data")

    settings = Settings()
    app_session = AppSession(settings, base_dir)
    session_id = app_session.history_manager.start_session(base_dir)

    # Move original file to safety storage branch folder
    branch_dir = os.path.join(base_dir, ".branches", session_id)
    os.makedirs(branch_dir, exist_ok=True)
    trapped_file = os.path.join(branch_dir, "document.pdf")
    shutil.move(file_src, trapped_file)

    # Attempt rollback while mocking resilient_move to always raise PermissionError
    with patch(
        "app.core.resilient_file_ops.resilient_move",
        side_effect=PermissionError("Destination locked"),
    ):
        with pytest.raises(PermissionError) as exc_info:
            app_session.rollback(session_id, True)

        assert "Recovery incomplete" in str(exc_info.value)
        app_session.close()

    # Verify safety storage branch folder and trapped file are preserved
    assert os.path.exists(branch_dir)
    assert os.path.exists(trapped_file)
    # The unfinished session stays in the store, so the next start offers recovery.
    assert any(s["session_id"] == session_id for s in find_abandoned_sessions())


def test_trapped_files_redetected_on_subsequent_scan(tmp_path, mock_session_base):
    """Test that trapped files in .branches/<sid> remain detected on subsequent application launches."""
    base_dir = str(tmp_path / "work_dir")
    os.makedirs(base_dir, exist_ok=True)

    file_src = os.path.join(base_dir, "report.docx")
    with open(file_src, "w") as f:
        f.write("docx data")

    settings = Settings()
    app_session = AppSession(settings, base_dir)
    session_id = app_session.history_manager.start_session(base_dir)

    branch_dir = os.path.join(base_dir, ".branches", session_id)
    os.makedirs(branch_dir, exist_ok=True)
    trapped_file = os.path.join(branch_dir, "report.docx")
    with open(trapped_file, "w") as f:
        f.write("docx data in safety storage")

    abandoned = find_abandoned_sessions()
    assert len(abandoned) >= 1
    session_match = next((s for s in abandoned if s["session_id"] == session_id), None)
    assert session_match is not None
    assert session_match["has_trapped_files"] is True
    assert session_match["safety_folder"] == branch_dir


def test_recovery_completes_and_cleans_up_after_fixing_permissions(
    tmp_path, mock_session_base, monkeypatch
):
    """Test that once permissions are restored, re-running recovery completes and cleans up session and safety storage."""
    monkeypatch.setattr("app.core.resilient_file_ops.RETRY_DELAY", 0.001)
    base_dir = str(tmp_path / "work_dir")
    os.makedirs(base_dir, exist_ok=True)

    file_src = os.path.join(base_dir, "photo.jpg")
    with open(file_src, "w") as f:
        f.write("jpg bytes")

    settings = Settings()
    app_session = AppSession(settings, base_dir)
    session_id = app_session.history_manager.start_session(base_dir)

    branch_dir = os.path.join(base_dir, ".branches", session_id)
    os.makedirs(branch_dir, exist_ok=True)
    trapped_file = os.path.join(branch_dir, "photo.jpg")
    shutil.move(file_src, trapped_file)

    # 1. Rollback fails first time
    with patch(
        "app.core.resilient_file_ops.resilient_move",
        side_effect=PermissionError("Access denied"),
    ):
        with pytest.raises(PermissionError):
            app_session.rollback(session_id, True)

    assert os.path.exists(branch_dir)

    # 2. Re-run rollback without error (permissions restored)
    app_session.rollback(session_id, True)
    app_session.close()

    # Safety storage directory .branches/<sid> should now be cleaned up by rollback
    assert not os.path.exists(branch_dir)
    assert os.path.exists(file_src)


def test_tui_do_recovery_rollback_failure_preserves_session_dir(mock_session_base):
    """Test that TUI _do_recovery_rollback preserves session directory and notifies user when rollback fails."""
    app = AutoSorterTUI(settings=Settings())
    app.announce = MagicMock()

    session_id = "test_sess_123"
    session_dir = mock_session_base / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    session_info = {
        "session_id": session_id,
        "base_dir": str(mock_session_base),
        "session_dir": str(session_dir),
    }

    with patch("app.core.session.AppSession") as mock_app_session_cls:
        mock_instance = MagicMock()
        history = mock_instance.history_manager
        history.get_sessions.return_value = [
            {"session_id": session_id, "base_dir": str(mock_session_base)}
        ]
        history.moves.list_moves.return_value = []
        history.rollback.side_effect = PermissionError("Permission error")
        mock_app_session_cls.return_value = mock_instance

        app._do_recovery_rollback(session_info)

    assert session_dir.exists()
    app.announce.assert_called()
    announcement = app.announce.call_args[0][0]
    assert "failed" in announcement.lower() or "preserved" in announcement.lower()


def test_session_startup_and_failed_transition_log_warnings_with_tracebacks(
    tmp_path, mock_session_base, caplog
):
    """Verify AppSession logs warnings with tracebacks for JEV db init, startup recovery, and FAILED transition errors."""
    import logging

    from app.core.moves import MoveLedger
    from app.core.pipeline import PipelineLifecycleState
    from app.core.shared_registry import SharedModelRegistry

    base_dir = str(tmp_path / "work_dir")
    os.makedirs(base_dir, exist_ok=True)
    doc_path = os.path.join(base_dir, "sample.txt")
    with open(doc_path, "w", encoding="utf-8") as fh:
        fh.write("hello")

    settings = Settings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    mock_jev = MagicMock()
    mock_jev.set_database.side_effect = RuntimeError("jev db attach failed")

    caplog.set_level(logging.WARNING, logger="app.core.session")
    with (
        patch.object(
            SharedModelRegistry.get_instance(),
            "get_jev_classifier",
            return_value=mock_jev,
        ),
        patch.object(
            MoveLedger,
            "reconcile_incomplete_transactions",
            side_effect=RuntimeError("startup reconcile failed"),
        ),
    ):
        app_session = AppSession(settings, base_dir)

    try:
        jev_records = [
            r
            for r in caplog.records
            if "JEV classifier" in r.getMessage() and r.levelno == logging.WARNING
        ]
        rec_records = [
            r
            for r in caplog.records
            if "reconcile incomplete move transactions" in r.getMessage()
            and r.levelno == logging.WARNING
        ]
        assert len(jev_records) == 1
        assert jev_records[0].exc_info is not None
        assert len(rec_records) == 1
        assert rec_records[0].exc_info is not None

        app_session.lifecycle_state = PipelineLifecycleState.PLANNED
        plan = {
            "Docs": {
                "sample.txt": {
                    "__type__": "file",
                    "filepath": doc_path,
                    "target_filename": "sample.txt",
                }
            }
        }
        orig_transition = app_session.transition_to

        def flaky_transition(target_state, context=None):
            if target_state == PipelineLifecycleState.FAILED:
                raise RuntimeError("transition to FAILED boom")
            return orig_transition(target_state, context=context)

        caplog.clear()
        with (
            patch(
                "app.core.mover.execute_moves",
                side_effect=OSError("simulated move failure"),
            ),
            patch.object(app_session, "transition_to", side_effect=flaky_transition),
        ):
            with pytest.raises(OSError, match="simulated move failure"):
                app_session.execute_moves(plan)

        fail_trans_records = [
            r
            for r in caplog.records
            if "lifecycle state to FAILED" in r.getMessage()
            and r.levelno == logging.WARNING
        ]
        assert len(fail_trans_records) == 1
        assert fail_trans_records[0].exc_info is not None
    finally:
        app_session.close()


def test_mover_logs_resume_flush_and_transition_errors_and_still_unwinds(
    tmp_path, caplog
):
    """Verify execute_moves logs resume lookup/registration, batch flush, and pipeline transition errors while still running unwind_session."""
    import logging

    from app.core.mover import execute_moves
    from app.core.pipeline import PipelineLifecycleState

    base_dir = tmp_path / "mover_diag"
    base_dir.mkdir(parents=True)
    f1 = base_dir / "a.txt"
    f2 = base_dir / "b.txt"
    f1.write_text("alpha", encoding="utf-8")
    f2.write_text("beta", encoding="utf-8")

    # 1. Resume session lookup + fallback start_session failure + cancel transition failure
    resume_history = MagicMock()
    resume_history.get_sessions.side_effect = RuntimeError("sessions lookup failed")
    resume_history.start_session.side_effect = RuntimeError("start_session failed")
    cancel_pipeline = MagicMock()
    cancel_pipeline.state = PipelineLifecycleState.EXECUTING
    cancel_pipeline.transition_to.side_effect = RuntimeError("cancel transition failed")

    plan_resume = {
        "Sorted": {
            "a.txt": {
                "__type__": "file",
                "filepath": str(f1),
                "target_filename": "a.txt",
            }
        }
    }
    caplog.set_level(logging.WARNING, logger="app.core.mover")
    cancel_summary = execute_moves(
        str(base_dir),
        plan_resume,
        db=None,
        history_manager=resume_history,
        resume=True,
        chunk_size=1,
        max_workers=1,
        cancel_check=lambda: True,
        pipeline=cancel_pipeline,
    )
    assert cancel_summary["cancelled"] is True
    mover_msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "Failed to look up active sort session" in mover_msgs
    assert "Failed to register sort session while resuming" in mover_msgs
    assert "Failed to transition pipeline to CANCELLED" in mover_msgs

    # 2. Flush error during failure must still run unwind_session and re-raise original error
    caplog.clear()
    fail_history = MagicMock()
    fail_history.start_session.return_value = "sess_flush_test"
    fail_history.moves = None

    flaky_db = MagicMock()
    flaky_db.get_document_hashes.return_value = {}
    flaky_db.execute_batch_updates.side_effect = RuntimeError("batch flush failed")

    fail_pipeline = MagicMock()
    fail_pipeline.state = PipelineLifecycleState.EXECUTING
    fail_pipeline.transition_to.side_effect = RuntimeError("pipeline transition failed")

    plan_two_chunks = {
        "Sorted": {
            "a.txt": {
                "__type__": "file",
                "filepath": str(f1),
                "target_filename": "a.txt",
            },
            "b.txt": {
                "__type__": "file",
                "filepath": str(f2),
                "target_filename": "b.txt",
            },
        }
    }
    from app.core import mover

    real_process_item = mover._process_move_item
    call_count = [0]

    def fail_second_item(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] >= 2:
            raise PermissionError("second chunk locked")
        return real_process_item(*args, **kwargs)

    with patch(
        "app.core.mover._process_move_item",
        side_effect=fail_second_item,
    ):
        with pytest.raises(PermissionError, match="second chunk locked"):
            execute_moves(
                str(base_dir),
                plan_two_chunks,
                db=flaky_db,
                history_manager=fail_history,
                batch_size=50,
                chunk_size=1,
                max_workers=1,
                pipeline=fail_pipeline,
            )

    fail_history.unwind_session.assert_called_once_with("sess_flush_test", db=flaky_db)
    fail_msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "Failed to flush pending database updates before rollback" in fail_msgs
    assert "Failed to transition pipeline to ROLLING_BACK" in fail_msgs
    assert "Failed to transition pipeline to FAILED" in fail_msgs
