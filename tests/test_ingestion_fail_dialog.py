import asyncio
from unittest.mock import MagicMock, patch
import pytest
from app.config import AppSettings
from app.ui.app import AutoSorterApp


@pytest.mark.anyio
async def test_scan_and_process_worker_failures_modal(mocker):
    # Set up settings and app
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    app = AutoSorterApp(settings)
    app.base_dir = "/dummy/base"
    
    # Mock AppSession and its methods
    mock_session = MagicMock()
    app.app_session = mock_session
    mock_session.base_dir = "/dummy/base"
    
    # Mock get_files_recursively
    mock_get_files = mocker.patch("app.core.scanner.get_files_recursively", return_value=["file1.txt", "file2.pdf", "file3.jpg"])
    
    # Chunk with 1 successful file, 1 unsupported, and 1 failed
    mock_chunk = {
        "file1.txt": {"text": "Hello, some valid extracted text.", "hash": "h1"},
        "file2.pdf": {"text": "[STATUS:UNSUPPORTED]", "hash": "h2"},
        "file3.jpg": {"text": "[STATUS:FAILED]", "hash": "h3"},
    }
    
    # Mock process_items generator
    def mock_process_items(items, callback, cancel_check):
        # Trigger the callback for progress tracking
        if callback:
            for _ in items:
                callback()
        yield mock_chunk

    mock_session.process_items = mock_process_items
    
    # Mock generate_sorting_plan
    mock_session.generate_sorting_plan = MagicMock(return_value={})
    
    # Mock other UI methods to avoid issues
    app.status_label = MagicMock()
    app.progress_bar = MagicMock()
    app.cancel_btn = MagicMock()
    app.execute_btn = MagicMock()
    app.render_tree = MagicMock()
    
    # Mock NiceGUI components
    mock_dialog_instance = MagicMock()
    
    # Custom mock context manager for dialog
    class MockDialogCtx:
        def __enter__(self):
            return mock_dialog_instance
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass
            
    mock_dialog = mocker.patch("app.ui.app.ui.dialog", return_value=MockDialogCtx())
    mock_card = mocker.patch("app.ui.app.ui.card")
    mock_scroll_area = mocker.patch("app.ui.app.ui.scroll_area")
    mock_row = mocker.patch("app.ui.app.ui.row")
    mock_column = mocker.patch("app.ui.app.ui.column")
    mock_label = mocker.patch("app.ui.app.ui.label")
    mock_button = mocker.patch("app.ui.app.ui.button")
    
    # Run the worker
    await app._scan_and_process_worker()
    
    # Assertions
    mock_dialog.assert_called_once()
    mock_dialog_instance.open.assert_called_once()
    
    # Verify that the correct failed files were displayed
    label_calls = [call[0][0] for call in mock_label.call_args_list]
    assert "Ingestion Failures" in label_calls
    assert "file2.pdf" in label_calls
    assert "Unsupported file format" in label_calls
    assert "file3.jpg" in label_calls
    assert "Extraction failed" in label_calls
    
    # Success files shouldn't be listed as failures
    assert "file1.txt" not in label_calls


@pytest.mark.anyio
async def test_scan_and_process_worker_no_failures(mocker):
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    app = AutoSorterApp(settings)
    app.base_dir = "/dummy/base"
    
    mock_session = MagicMock()
    app.app_session = mock_session
    mock_session.base_dir = "/dummy/base"
    
    mock_get_files = mocker.patch("app.core.scanner.get_files_recursively", return_value=["file1.txt"])
    
    mock_chunk = {
        "file1.txt": {"text": "Hello, some valid extracted text.", "hash": "h1"},
    }
    
    def mock_process_items(items, callback, cancel_check):
        if callback:
            for _ in items:
                callback()
        yield mock_chunk

    mock_session.process_items = mock_process_items
    mock_session.generate_sorting_plan = MagicMock(return_value={})
    
    app.status_label = MagicMock()
    app.progress_bar = MagicMock()
    app.cancel_btn = MagicMock()
    app.execute_btn = MagicMock()
    app.render_tree = MagicMock()
    
    mock_dialog = mocker.patch("app.ui.app.ui.dialog")
    
    await app._scan_and_process_worker()
    
    mock_dialog.assert_not_called()
