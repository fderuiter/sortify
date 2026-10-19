"""Tests for live folder capacity banner in TUI."""

from unittest.mock import AsyncMock

import pytest

from app.config import AppSettings
from app.ui.tui import AutoSorterTUI


@pytest.fixture(autouse=True)
def isolated_app_dir(monkeypatch, tmp_path):
    """Isolate AppSettings from persistent disk state."""
    import app.config
    import app.core.session
    from app.core.plugin_registry import PluginRegistry

    PluginRegistry.reset_instance()
    app.config.AppSettings.clear_observers()
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setattr(app.config, "get_app_dir", lambda: tmp_path)
    monkeypatch.setattr(
        app.config.AppSettings, "_trigger_save", lambda self: self._save()
    )
    monkeypatch.setattr(
        app.core.session,
        "scan_abandoned_sessions_async",
        AsyncMock(return_value=[]),
    )


def test_capacity_meter_in_header():
    """Verify live folder capacity meter calculation and sub_title display."""
    settings = AppSettings()
    settings.MAX_FOLDERS = 12
    app = AutoSorterTUI(settings=settings)

    # Initial empty plan
    assert app._get_folder_count() == 0
    app.update_capacity_meter()
    assert app.sub_title == "0 / 12 Subdirectories used"

    # Add 4 subdirectories to plan
    app.plan = {
        "Invoices": {
            "inv1.pdf": {"__type__": "file", "filepath": "/dummy/inv1.pdf"}
        },
        "Contracts": {
            "c1.docx": {"__type__": "file", "filepath": "/dummy/c1.docx"}
        },
        "Reports": {},
        "Archives": {},
    }
    app.rebuild_tree()

    assert app._get_folder_count() == 4
    assert app.sub_title == "4 / 12 Subdirectories used"
