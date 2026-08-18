import pytest
from nicegui import Client, context
from pydantic import ValidationError

from app.config import AppSettings, Settings
from app.core.daemon import ContinuousWatchdogDaemon
from app.ui.app import AutoSorterApp
from app.ui.settings import show_settings


def test_ignored_extensions_defaults():
    """Verify default ignored extensions in fresh Settings instance."""
    settings = Settings()
    assert settings.IGNORED_EXTENSIONS == [".crdownload", ".tmp", ".download"]


def test_ignored_extensions_automatic_leading_dot_formatting():
    """Inputs missing a leading dot should automatically receive standard leading-dot formatting."""
    settings = Settings(IGNORED_EXTENSIONS=["crdownload", "tmp", ".download", "part"])
    assert settings.IGNORED_EXTENSIONS == [".crdownload", ".tmp", ".download", ".part"]


def test_ignored_extensions_rejects_empty_or_whitespace():
    """Empty or whitespace-only extension entries must raise a validation error."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(IGNORED_EXTENSIONS=[""])
    assert "blank or whitespace-only" in str(exc_info.value)

    with pytest.raises(ValidationError) as exc_info2:
        Settings(IGNORED_EXTENSIONS=["   "])
    assert "blank or whitespace-only" in str(exc_info2.value)

    with pytest.raises(ValidationError) as exc_info3:
        Settings(IGNORED_EXTENSIONS=["."])
    assert "blank or whitespace-only" in str(exc_info3.value)


def test_app_settings_runtime_validation(tmp_path):
    """Runtime assignment of invalid extensions must raise ValidationError and leave settings unchanged."""
    filepath = tmp_path / "settings.json"
    app_settings = AppSettings(filepath=str(filepath))

    # Initial state
    assert app_settings.IGNORED_EXTENSIONS == [".crdownload", ".tmp", ".download"]

    # Valid update with missing leading dots
    app_settings.IGNORED_EXTENSIONS = [".crdownload", "bak"]
    assert app_settings.IGNORED_EXTENSIONS == [".crdownload", ".bak"]

    # Invalid update - empty string
    with pytest.raises(ValidationError):
        app_settings.IGNORED_EXTENSIONS = [""]
    assert app_settings.IGNORED_EXTENSIONS == [".crdownload", ".bak"]

    # Invalid update - whitespace string
    with pytest.raises(ValidationError):
        app_settings.IGNORED_EXTENSIONS = ["   "]
    assert app_settings.IGNORED_EXTENSIONS == [".crdownload", ".bak"]

    if app_settings._save_timer:
        app_settings._save_timer.cancel()


def test_immediate_effect_on_daemon(tmp_path):
    """Saved extension updates take effect immediately for background file detection."""
    app_settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    daemon = ContinuousWatchdogDaemon(app_settings, str(tmp_path))

    # Initially .bak is not ignored
    assert daemon.should_ignore_path("document.bak") is False
    assert daemon.should_ignore_path("file.tmp") is True

    # Update settings
    app_settings.IGNORED_EXTENSIONS = [".tmp", ".bak"]

    # Immediately reflected in daemon
    assert daemon.should_ignore_path("document.bak") is True
    assert daemon.should_ignore_path("archive.download") is False

    if app_settings._save_timer:
        app_settings._save_timer.cancel()


def _click_button(btn):
    """Trigger click event on a NiceGUI button."""
    if hasattr(btn, "_event_listeners"):
        listeners = list(btn._event_listeners.values())
        for listener in listeners:
            if getattr(listener, "type", "") == "click":
                listener.handler(None)


def test_ui_ignored_extensions_rendering_and_actions():
    """Test UI rendering, adding with auto-formatting, validation error handling, and removal."""
    settings = Settings()
    app = AutoSorterApp(settings)

    with Client(None):
        context.client.elements.clear()
        show_settings(app, settings)

        # Locate UI elements in runtime elements tree
        elements = list(context.client.elements.values())

        # Find labels matching ignored extensions
        labels = [
            e
            for e in elements
            if getattr(e, "text", "") in [".crdownload", ".tmp", ".download"]
            or getattr(e, "_text", "") in [".crdownload", ".tmp", ".download"]
        ]
        assert len(labels) == 3

        # Find the Add button for Ignored Extension
        add_buttons = [
            e
            for e in elements
            if getattr(e, "_props", {}).get("aria-label")
            == "Add Ignored Extension Button"
        ]
        assert len(add_buttons) == 1

        # Find input for Add Ignored Extension
        input_elements = [
            e
            for e in elements
            if "Add Ignored Extension input"
            in str(getattr(e, "_props", {}).get("aria-label", ""))
        ]
        assert len(input_elements) == 1
        ext_input = input_elements[0]

        # 1. Test adding extension without leading dot ("bak")
        ext_input.value = "bak"
        add_button = add_buttons[0]
        _click_button(add_button)

        assert ".bak" in settings.IGNORED_EXTENSIONS

        # 2. Test attempting to add empty/whitespace extension
        ext_input.value = "   "
        _click_button(add_button)
        # Ensure setting was NOT modified to contain whitespace or empty entry
        assert "" not in settings.IGNORED_EXTENSIONS
        assert "   " not in settings.IGNORED_EXTENSIONS

        # 3. Test removing extension via UI button click
        remove_buttons = [
            e
            for e in list(context.client.elements.values())
            if getattr(e, "text", "") == "Remove"
            or getattr(e, "_props", {}).get("label") == "Remove"
        ]
        assert len(remove_buttons) >= 4
        _click_button(remove_buttons[-1])
        assert ".bak" not in settings.IGNORED_EXTENSIONS
