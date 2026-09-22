"""Tests for setup wizard and app initialization."""

from app.config import AppSettings
from app.ui.app import AutoSorterApp
from app.ui.wizard import show_wizard


def test_show_wizard_callable(tmp_path):
    settings = AppSettings()
    app = AutoSorterApp(settings)
    app.build_ui()
    # Ensure show_wizard can be invoked safely without nicegui
    show_wizard(app, settings)
    assert True
