import pytest
from pydantic import ValidationError

from app.config import AppSettings, Settings
from app.core.daemon import ContinuousWatchdogDaemon


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
