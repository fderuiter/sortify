import json
import logging
import os
import time
from unittest.mock import MagicMock, patch

import pytest
from app.config import AppSettings, Settings


def test_revalidate_unlocks_auto_save_and_persists(tmp_path):
    """Test that correcting invalid settings and calling revalidate clears error flags, unlocks auto-saving, and saves to disk."""
    config_file = tmp_path / "settings.json"
    # Write invalid config file with out-of-bounds MAX_WORKERS
    invalid_data = {"MAX_WORKERS": 999, "PROXY": "http://127.0.0.1:8080"}
    config_file.write_text(json.dumps(invalid_data), encoding="utf-8")

    settings = AppSettings(filepath=str(config_file))
    assert settings._has_validation_errors is True
    assert len(settings._validation_errors) > 0

    # Auto-saving should be locked
    settings._save()  # Should not crash or overwrite with invalid data, or trigger_save skips
    # Fixing the field via __setattr__ triggers revalidate automatically
    settings.MAX_WORKERS = 4

    assert settings._has_validation_errors is False
    assert settings._validation_errors == []

    # Give save timer time to execute or call _save
    if settings._save_timer:
        settings._save_timer.cancel()
    settings._save()

    # Verify updated settings file on disk
    saved_data = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved_data["MAX_WORKERS"] == 4
    # Proxy should be encrypted on save
    assert saved_data["PROXY"].startswith("enc:")


def test_revalidate_returns_false_if_errors_remain(tmp_path):
    """Test that revalidate returns False and keeps saving locked if errors remain."""
    config_file = tmp_path / "settings.json"
    invalid_data = {"MAX_WORKERS": 999}
    config_file.write_text(json.dumps(invalid_data), encoding="utf-8")

    settings = AppSettings(filepath=str(config_file))
    assert settings._has_validation_errors is True

    # Set an invalid protected path directly on settings_model to test validation failure in revalidate()
    object.__setattr__(settings._settings_model, "PROTECTED_PATHS", ["relative/path"])
    success = settings.revalidate()

    assert success is False
    assert settings._has_validation_errors is True
    assert len(settings._validation_errors) > 0


def test_proxy_encryption_preserved_during_revalidate_and_save(tmp_path):
    """Test that proxy credentials preserve encryption during re-validation and auto-save."""
    config_file = tmp_path / "settings.json"
    app_settings = AppSettings(filepath=str(config_file))

    app_settings.PROXY = "http://admin:secret123@proxy.example.com:8080"
    assert app_settings.PROXY == "http://admin:secret123@proxy.example.com:8080"

    res = app_settings.revalidate()
    assert res is True

    if app_settings._save_timer:
        app_settings._save_timer.cancel()
    app_settings._save()

    content = config_file.read_text(encoding="utf-8")
    saved_json = json.loads(content)
    assert saved_json["PROXY"].startswith("enc:")

    # Reload from disk and verify decryption
    reloaded = AppSettings(filepath=str(config_file))
    assert reloaded.PROXY == "http://admin:secret123@proxy.example.com:8080"
    assert reloaded._has_validation_errors is False


def test_ui_revalidate_button_clears_banner_and_unlocks(tmp_path):
    """Test UI warning banner rendering and Re-validate button interaction."""
    from app.ui.settings import render_validation_warning_banner

    config_file = tmp_path / "settings.json"
    config_file.write_text(json.dumps({"MAX_WORKERS": 999}), encoding="utf-8")

    settings = AppSettings(filepath=str(config_file))
    assert settings._has_validation_errors is True

    mock_buttons = []
    revalidate_callback = None

    def mock_button(text="", on_click=None, **kwargs):
        btn = MagicMock()
        btn.text = text
        btn.props = MagicMock(return_value=btn)
        btn.classes = MagicMock(return_value=btn)
        if text == "Re-validate":
            nonlocal revalidate_callback
            revalidate_callback = on_click
        mock_buttons.append(btn)
        return btn

    mock_card_instance = MagicMock()
    mock_card_instance.classes.return_value = mock_card_instance

    with (
        patch("nicegui.ui.card", return_value=mock_card_instance),
        patch("nicegui.ui.row", return_value=MagicMock()),
        patch("nicegui.ui.label", return_value=MagicMock()),
        patch("nicegui.ui.icon", return_value=MagicMock()),
        patch("nicegui.ui.link", return_value=MagicMock()),
        patch("nicegui.ui.button", side_effect=mock_button),
        patch("nicegui.ui.notify") as mock_notify,
    ):
        banner = render_validation_warning_banner(settings)

        # Fix setting to valid value
        settings.MAX_WORKERS = 4
        assert settings._has_validation_errors is False

        # Execute re-validate click
        assert revalidate_callback is not None
        revalidate_callback()

        mock_notify.assert_called()
        mock_card_instance.set_visibility.assert_called_with(False)
