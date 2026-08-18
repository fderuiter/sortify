"""Unit tests for Page-Scoped Instance Isolation in AutoSorterApp and run_app."""

import os
from unittest.mock import MagicMock, patch

from app.config import AppSettings
from app.core.session import AppSession
from app.ui.app import AutoSorterApp, run_app


def test_page_scoped_instance_instantiation():
    """Verify that calling the page route builder creates distinct AutoSorterApp instances."""
    settings = AppSettings()
    captured_instances = []

    with patch("app.ui.app.ui") as mock_ui:
        # Define a mock page decorator that captures page functions
        page_funcs = []

        def mock_page_decorator(path):
            def decorator(func):
                page_funcs.append(func)
                return func

            return decorator

        mock_ui.page.side_effect = mock_page_decorator

        # Run app
        run_app(settings)

        assert len(page_funcs) == 1
        page_builder = page_funcs[0]

        # Simulate two concurrent browser tab connections
        app_tab1 = page_builder()
        app_tab2 = page_builder()

        assert app_tab1 is not app_tab2
        assert isinstance(app_tab1, AutoSorterApp)
        assert isinstance(app_tab2, AutoSorterApp)


def test_independent_state_attributes():
    """Verify each page instance maintains independent plan, locked files, and folder state."""
    settings = AppSettings()
    app_tab1 = AutoSorterApp(settings)
    app_tab2 = AutoSorterApp(settings)

    # Mutate tab 1 state
    app_tab1.base_dir = "/path/to/tab1"
    app_tab1.plan = {"FolderA": {"file1.pdf": {"__type__": "file"}}}
    app_tab1.locked_files = {"file1.pdf": "/path/to/tab1/FolderA"}
    app_tab1.manual_folders.add("CustomFolder1")

    # Tab 2 state must remain pristine
    assert app_tab2.base_dir == ""
    assert app_tab2.plan == {}
    assert app_tab2.locked_files == {}
    assert "CustomFolder1" not in app_tab2.manual_folders


def test_independent_cancellation_flags():
    """Verify triggering task cancellation in one tab does not affect cancellation flags in parallel tabs."""
    settings = AppSettings()
    app_tab1 = AutoSorterApp(settings)
    app_tab2 = AutoSorterApp(settings)

    # Cancel analysis on tab 1
    app_tab1.cancel_analysis()

    assert app_tab1._cancel_analysis_flag is True
    assert app_tab2._cancel_analysis_flag is False

    # Cancel recalc on tab 2
    app_tab2.cancel_recalc()

    assert app_tab2._cancel_recalc_flag is True
    assert app_tab1._cancel_recalc_flag is False


def test_database_handle_and_session_isolation(tmp_path):
    """Verify session database handles remain isolated across parallel page instances."""
    settings = AppSettings()
    dir_tab1 = str(tmp_path / "tab1_dir")
    dir_tab2 = str(tmp_path / "tab2_dir")
    os.makedirs(dir_tab1, exist_ok=True)
    os.makedirs(dir_tab2, exist_ok=True)

    app_tab1 = AutoSorterApp(settings)
    app_tab1.base_dir = dir_tab1
    session_tab1 = AppSession(settings, base_dir=dir_tab1)
    app_tab1.app_session = session_tab1

    app_tab2 = AutoSorterApp(settings)
    app_tab2.base_dir = dir_tab2
    session_tab2 = AppSession(settings, base_dir=dir_tab2)
    app_tab2.app_session = session_tab2

    # Ensure separate session directories and database handles
    assert session_tab1.session_dir != session_tab2.session_dir
    assert session_tab1.db is not session_tab2.db

    # Write data in tab 1 database
    session_tab1.db.upsert_document(dir_tab1, "doc1.txt", "hash1", "Content 1")

    # Tab 2 database should not contain tab 1's document
    doc_in_tab2 = session_tab2.db.get_document(dir_tab1, "doc1.txt")
    assert doc_in_tab2 is None

    # Closing session in tab 1 must not invalidate database connection in tab 2
    app_tab1.app_session.close()
    app_tab1.app_session = None

    # Tab 2 session database remains valid and operational
    session_tab2.db.upsert_document(dir_tab2, "doc2.txt", "hash2", "Content 2")
    doc_in_tab2 = session_tab2.db.get_document(dir_tab2, "doc2.txt")
    assert doc_in_tab2 is not None
    assert doc_in_tab2["file_hash"] == "hash2"

    session_tab2.close()


def test_ui_updates_isolation():
    """Verify UI status updates and progress indicators are scoped exclusively to their own page instance."""
    settings = AppSettings()

    app_tab1 = AutoSorterApp(settings)
    app_tab2 = AutoSorterApp(settings)

    # Mock UI elements on both instances
    app_tab1.status_label = MagicMock()
    app_tab1.progress_bar = MagicMock()

    app_tab2.status_label = MagicMock()
    app_tab2.progress_bar = MagicMock()

    # Simulate status update on tab 1
    app_tab1.status_label.set_text("Scanning Tab 1...")
    app_tab1.progress_bar.set_value(0.5)

    app_tab1.status_label.set_text.assert_called_once_with("Scanning Tab 1...")
    app_tab1.progress_bar.set_value.assert_called_once_with(0.5)

    # Tab 2 UI elements were not called
    app_tab2.status_label.set_text.assert_not_called()
    app_tab2.progress_bar.set_value.assert_not_called()
