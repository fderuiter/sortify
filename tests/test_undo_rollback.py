import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings
from app.core.session import AppSession
from app.ui.app import AutoSorterApp


@pytest.mark.anyio
async def test_undo_last_sort_no_history(tmp_path):
    """Verify that attempting to undo without prior history shows an informative notification without crashing."""
    settings = AppSettings()
    app = AutoSorterApp(settings)
    app.base_dir = str(tmp_path)
    app.status_label = MagicMock()
    app.progress_bar = MagicMock()
    app.undo_btn = MagicMock()

    with patch("app.ui.app.ui") as mock_ui:
        await app.undo_last_sort()

        mock_ui.notify.assert_called_once_with(
            "No sort history available to undo.", type="info"
        )
        assert app.status_label.set_text.call_count >= 1


@pytest.mark.anyio
async def test_undo_last_sort_success(tmp_path):
    """Verify that clicking undo restores moved files to their pre-sort locations and triggers rescan."""
    settings = AppSettings()
    base_dir = str(tmp_path / "workspace")
    os.makedirs(base_dir, exist_ok=True)

    test_file = os.path.join(base_dir, "document.txt")
    with open(test_file, "w") as f:
        f.write("Sample content for undo test")

    app = AutoSorterApp(settings)
    app.base_dir = base_dir
    app.status_label = MagicMock()
    app.progress_bar = MagicMock()
    app.undo_btn = MagicMock()

    # Create app session
    session = AppSession(settings, base_dir)
    app.app_session = session

    # Define plan to move document.txt into SortedFolder
    target_folder = os.path.join(base_dir, "SortedFolder")
    os.makedirs(target_folder, exist_ok=True)
    plan = {
        "SortedFolder": {
            "document.txt": {
                "__type__": "file",
                "source_path": test_file,
                "relative_source": "../document.txt",
                "target_filename": "document.txt",
            }
        }
    }

    # Execute moves to create snapshot and perform move
    session.execute_moves(plan)
    assert not os.path.exists(test_file)
    assert os.path.exists(os.path.join(target_folder, "document.txt"))

    with patch("app.ui.app.ui") as mock_ui, patch.object(app, "start_analysis") as mock_start_analysis:
        await app.undo_last_sort()

        # File should be restored to pre-sort location
        assert os.path.exists(test_file)
        assert not os.path.exists(os.path.join(target_folder, "document.txt"))

        # Check positive notification
        mock_ui.notify.assert_called_with(
            "Rollback completed successfully! Files restored.", type="positive"
        )
        # Check that start_analysis (directory rescan) was called
        mock_start_analysis.assert_called_once()


@pytest.mark.anyio
async def test_undo_last_sort_failure_handling(tmp_path):
    """Verify graceful handling of errors during rollback."""
    settings = AppSettings()
    base_dir = str(tmp_path)

    app = AutoSorterApp(settings)
    app.base_dir = base_dir
    app.status_label = MagicMock()
    app.progress_bar = MagicMock()
    app.undo_btn = MagicMock()

    session = AppSession(settings, base_dir)
    # Create fake session in history manager so get_sessions returns something
    session.history_manager.get_sessions = MagicMock(
        return_value=[{"session_id": "fake_session", "base_dir": base_dir, "status": "active"}]
    )
    session.rollback = MagicMock(side_effect=RuntimeError("Simulated rollback error"))
    app.app_session = session

    with patch("app.ui.app.ui") as mock_ui:
        await app.undo_last_sort()

        mock_ui.notify.assert_called_once_with(
            "Rollback failed: Simulated rollback error", type="negative"
        )
        app.status_label.set_text.assert_called_with("Rollback failed.")
