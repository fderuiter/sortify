import asyncio
import os
import shutil
from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings as Settings
from app.core.session import AppSession, scan_abandoned_sessions_async
from app.ui.tui import AutoSorterTUI


@pytest.fixture
def mock_session_base(tmp_path, monkeypatch):
    """Isolate session base directory to tmp_path."""
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
    session_id = app_session.history_manager.create_snapshot(base_dir)
    session_dir = app_session.session_dir

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
    assert os.path.exists(session_dir)


def test_trapped_files_redetected_on_subsequent_scan(tmp_path, mock_session_base):
    """Test that trapped files in .branches/<sid> remain detected on subsequent application launches."""
    base_dir = str(tmp_path / "work_dir")
    os.makedirs(base_dir, exist_ok=True)

    file_src = os.path.join(base_dir, "report.docx")
    with open(file_src, "w") as f:
        f.write("docx data")

    settings = Settings()
    app_session = AppSession(settings, base_dir)
    session_id = app_session.history_manager.create_snapshot(base_dir)

    branch_dir = os.path.join(base_dir, ".branches", session_id)
    os.makedirs(branch_dir, exist_ok=True)
    trapped_file = os.path.join(branch_dir, "report.docx")
    with open(trapped_file, "w") as f:
        f.write("docx data in safety storage")

    abandoned = asyncio.run(scan_abandoned_sessions_async())
    assert len(abandoned) >= 1
    session_match = next((s for s in abandoned if s["session_id"] == session_id), None)
    assert session_match is not None
    assert session_match["has_trapped_files"] is True
    assert session_match["safety_folder"] == branch_dir


def test_recovery_completes_and_cleans_up_after_fixing_permissions(
    tmp_path, mock_session_base
):
    """Test that once permissions are restored, re-running recovery completes and cleans up session and safety storage."""
    base_dir = str(tmp_path / "work_dir")
    os.makedirs(base_dir, exist_ok=True)

    file_src = os.path.join(base_dir, "photo.jpg")
    with open(file_src, "w") as f:
        f.write("jpg bytes")

    settings = Settings()
    app_session = AppSession(settings, base_dir)
    session_id = app_session.history_manager.create_snapshot(base_dir)

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
        mock_instance.rollback.side_effect = PermissionError("Permission error")
        mock_app_session_cls.return_value = mock_instance

        app._do_recovery_rollback(session_info)

    assert session_dir.exists()
    app.announce.assert_called()
    announcement = app.announce.call_args[0][0]
    assert "failed" in announcement.lower() or "preserved" in announcement.lower()
