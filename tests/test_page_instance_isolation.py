"""Unit tests for Instance Isolation in AutoSorterApp."""

from app.config import AppSettings
from app.ui.app import AutoSorterApp


def test_page_scoped_instance_instantiation():
    """Verify that creating AutoSorterApp instances produces distinct isolated objects."""
    settings = AppSettings()
    app_tab1 = AutoSorterApp(settings)
    app_tab2 = AutoSorterApp(settings)

    assert app_tab1 is not app_tab2
    assert isinstance(app_tab1, AutoSorterApp)
    assert isinstance(app_tab2, AutoSorterApp)


def test_independent_state_attributes():
    """Verify each instance maintains independent plan, locked files, and folder state."""
    settings = AppSettings()
    app_tab1 = AutoSorterApp(settings)
    app_tab2 = AutoSorterApp(settings)

    # Mutate instance 1 state
    app_tab1.base_dir = "/path/to/tab1"
    app_tab1.plan = {"FolderA": {"file1.pdf": {"__type__": "file"}}}
    app_tab1.locked_files = {"file1.pdf": "/path/to/tab1/FolderA"}
    app_tab1.manual_folders.add("CustomFolder1")

    # Instance 2 state must remain pristine
    assert app_tab2.base_dir == ""
    assert app_tab2.plan == {}
    assert app_tab2.locked_files == {}
    assert "CustomFolder1" not in app_tab2.manual_folders
