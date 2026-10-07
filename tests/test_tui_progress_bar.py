"""Unit and integration tests for main view integrated progress bar controller in AutoSorterTUI."""

import asyncio
import os
from unittest.mock import MagicMock, patch

from textual.widgets import ProgressBar

from app.config import AppSettings
from app.core.progress import ProgressUpdate
from app.ui.tui import AutoSorterTUI


def test_autosorter_tui_compose_contains_progress_bar(tmp_path):
    """Verify AutoSorterTUI.compose contains a ProgressBar widget."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

        async with app.run_test() as pilot:
            pb = app.query_one("#main-progress-bar", ProgressBar)
            assert pb is not None
            assert isinstance(pb, ProgressBar)

    asyncio.run(_test())


def test_handle_progress_update_and_reset(tmp_path):
    """Verify _handle_progress_update updates ProgressBar and status bar, and reset_progress_bar resets state."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

        async with app.run_test() as pilot:
            pb = app.query_one("#main-progress-bar", ProgressBar)
            assert pb.display is False

            # Dispatch progress update with ratio 0.5
            app._handle_progress_update(0.5, stage="Scanning test_doc.pdf", unit_count=5, unit_type="files")
            await pilot.pause()

            assert pb.display is True
            assert pb.progress == 50.0

            sb = app.query_one("#status-bar")
            assert "Scanning test_doc.pdf" in str(sb.render())

            # Reset progress bar
            app.reset_progress_bar()
            await pilot.pause()

            assert pb.display is False
            assert pb.progress == 0.0

    asyncio.run(_test())


def test_handle_progress_update_object(tmp_path):
    """Verify _handle_progress_update accepts ProgressUpdate instances."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

        async with app.run_test() as pilot:
            pb = app.query_one("#main-progress-bar", ProgressBar)
            update = ProgressUpdate(progress=0.75, stage="Moving batch chunk 2", unit_count=15, unit_type="files")

            app._handle_progress_update(update)
            await pilot.pause()

            assert pb.display is True
            assert pb.progress == 75.0

            sb = app.query_one("#status-bar")
            assert "Moving batch chunk 2" in str(sb.render())

    asyncio.run(_test())


def test_run_scan_worker_passes_progress_callback(tmp_path):
    """Verify run_scan_worker passes a functional progress_callback to MetadataPass.run."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

        # Create dummy file
        test_file = os.path.join(str(tmp_path), "sample.txt")
        with open(test_file, "w") as f:
            f.write("test content")

        async with app.run_test() as pilot:
            with patch("app.core.metadata.MetadataPass.run") as mock_meta_run:
                worker = app.run_scan_worker()
                await worker.wait()

                assert mock_meta_run.called
                kwargs = mock_meta_run.call_args.kwargs
                assert "progress_callback" in kwargs
                assert callable(kwargs["progress_callback"])

    asyncio.run(_test())


def test_run_execute_worker_passes_progress_callback(tmp_path):
    """Verify run_execute_worker passes a functional progress_callback to AppSession.execute_moves."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        app.plan = {"Target": {"sample.txt": {"__type__": "file", "filepath": os.path.join(str(tmp_path), "sample.txt"), "target_filename": "sample.txt"}}}

        async with app.run_test() as pilot:
            mock_session = MagicMock()
            mock_session.execute_moves.return_value = {"moved": 1, "failed": 0}
            app.app_session = mock_session
            app.action_scan_directory = MagicMock()

            worker = app.run_execute_worker()
            try:
                await worker.wait()
            except Exception:
                pass

            assert mock_session.execute_moves.called
            kwargs = mock_session.execute_moves.call_args.kwargs
            assert "progress_callback" in kwargs
            assert callable(kwargs["progress_callback"])

    asyncio.run(_test())
