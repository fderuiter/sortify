import json
import logging

import pytest
from pydantic import ValidationError

from app.config import AppSettings, Settings


def test_valid_relative_paths():
    """Valid relative paths should be accepted without validation warnings."""
    settings = Settings(
        KEYWORD_RULES={"test": "valid/relative/path", "docs": "documents"}
    )
    assert settings.KEYWORD_RULES["test"] == "valid/relative/path"
    assert settings.KEYWORD_RULES["docs"] == "documents"


def test_reject_directory_traversal():
    """Paths containing '..' segments should be rejected."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(KEYWORD_RULES={"test": "../outside/path"})
    assert "directory traversal segments" in str(exc_info.value)

    with pytest.raises(ValidationError) as exc_info2:
        Settings(KEYWORD_RULES={"test": "folder/../../etc"})
    assert "directory traversal segments" in str(exc_info2.value)


def test_reject_absolute_paths():
    """Absolute paths should be rejected."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(KEYWORD_RULES={"test": "/absolute/path"})
    assert "absolute path" in str(exc_info.value)

    with pytest.raises(ValidationError) as exc_info2:
        Settings(KEYWORD_RULES={"test": "\\windows\\absolute\\path"})
    assert "absolute path" in str(exc_info2.value)


def test_reject_illegal_characters():
    """Paths containing illegal OS characters should be rejected."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(KEYWORD_RULES={"test": "C:\\fake\\path"})
    assert "illegal characters" in str(exc_info.value)

    with pytest.raises(ValidationError) as exc_info2:
        Settings(KEYWORD_RULES={"test": "folder/with<bad>chars"})
    assert "illegal characters" in str(exc_info2.value)


def test_fallback_mechanics(tmp_path, caplog):
    """When an invalid path is detected during load, the application reverts to default empty settings."""
    mock_filepath = tmp_path / "settings.json"
    invalid_data = {"KEYWORD_RULES": {"bad": "../traversal"}}
    mock_filepath.write_text(json.dumps(invalid_data))

    with caplog.at_level(logging.WARNING):
        app_settings = AppSettings(filepath=str(mock_filepath))

    # The invalid path should have triggered a fallback to default (empty dict)
    assert app_settings.KEYWORD_RULES == {}
    assert "Invalid KEYWORD_RULES in config, using default:" in caplog.text

    # Now check that valid data loads correctly
    valid_data = {"KEYWORD_RULES": {"good": "valid/path"}}
    mock_filepath.write_text(json.dumps(valid_data))
    app_settings.load()
    assert app_settings.KEYWORD_RULES == {"good": "valid/path"}


def test_runtime_validation_trigger(tmp_path):
    """Runtime assignments to KEYWORD_RULES automatically trigger validation."""
    mock_filepath = tmp_path / "settings.json"
    app_settings = AppSettings(filepath=str(mock_filepath))

    # Valid assignment works
    app_settings.KEYWORD_RULES = {"good": "valid/path"}
    assert app_settings.KEYWORD_RULES == {"good": "valid/path"}

    # Invalid assignment raises ValidationError and the state remains unchanged
    with pytest.raises(ValidationError):
        app_settings.KEYWORD_RULES = {"bad": "/absolute/path"}

    assert app_settings.KEYWORD_RULES == {"good": "valid/path"}

    # Cleanup the timer to avoid background thread noise in pytest
    if app_settings._save_timer:
        app_settings._save_timer.cancel()


def test_config_parameter_bounds():
    """Test that out-of-bounds configuration values are rejected."""
    # MAX_WORKERS: 1 to 64
    with pytest.raises(ValidationError):
        Settings(MAX_WORKERS=0)
    with pytest.raises(ValidationError):
        Settings(MAX_WORKERS=65)
    assert Settings(MAX_WORKERS=1).MAX_WORKERS == 1
    assert Settings(MAX_WORKERS=64).MAX_WORKERS == 64

    # MAX_FOLDERS: 1 to 50
    with pytest.raises(ValidationError):
        Settings(MAX_FOLDERS=0)
    with pytest.raises(ValidationError):
        Settings(MAX_FOLDERS=51)

    # MAX_DEPTH: 1 to 10
    with pytest.raises(ValidationError):
        Settings(MAX_DEPTH=0)
    with pytest.raises(ValidationError):
        Settings(MAX_DEPTH=11)

    # MAX_FEATURES: 1 to 10
    with pytest.raises(ValidationError):
        Settings(MAX_FEATURES=0)
    with pytest.raises(ValidationError):
        Settings(MAX_FEATURES=11)


def test_config_invalid_structures():
    """Test that invalid types/structures are rejected."""
    with pytest.raises(ValidationError):
        Settings(KEYWORD_RULES=[1, 2, 3])  # Should be a dict
    with pytest.raises(ValidationError):
        Settings(MAX_WORKERS="not an int")
    with pytest.raises(ValidationError):
        Settings(CLEANUP_EMPTY_FOLDERS="invalid bool")
