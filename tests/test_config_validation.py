import json
import logging
import os
from unittest.mock import patch

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

    # MODEL_THREADS: 1 to 32
    with pytest.raises(ValidationError):
        Settings(MODEL_THREADS=0)
    with pytest.raises(ValidationError):
        Settings(MODEL_THREADS=33)
    assert Settings(MODEL_THREADS=1).MODEL_THREADS == 1
    assert Settings(MODEL_THREADS=32).MODEL_THREADS == 32
    assert Settings().MODEL_THREADS == 2


def test_config_invalid_structures():
    """Test that invalid types/structures are rejected."""
    with pytest.raises(ValidationError):
        Settings(KEYWORD_RULES=[1, 2, 3])  # Should be a dict
    with pytest.raises(ValidationError):
        Settings(MAX_WORKERS="not an int")
    with pytest.raises(ValidationError):
        Settings(CLEANUP_EMPTY_FOLDERS="invalid bool")


def test_protected_paths_validation():
    """Test validation of protected paths setting."""
    # Default is empty list
    assert Settings().PROTECTED_PATHS == []

    # Valid absolute paths are allowed
    settings = Settings(PROTECTED_PATHS=["/absolute/path", "/another/absolute/path"])
    assert settings.PROTECTED_PATHS == ["/absolute/path", "/another/absolute/path"]

    # Relative paths are rejected
    with pytest.raises(ValidationError) as exc_info:
        Settings(PROTECTED_PATHS=["relative/path"])
    assert "absolute path" in str(exc_info.value)

    # Non-string types are rejected
    with pytest.raises(ValidationError) as exc_info:
        Settings(PROTECTED_PATHS=[123])
    assert "string" in str(exc_info.value)

    # Wildcard patterns are rejected
    for wildcard in ("*", "?", "[", "]"):
        with pytest.raises(ValidationError) as exc_info:
            Settings(PROTECTED_PATHS=[f"/absolute/path/with/{wildcard}"])
        assert "Wildcard" in str(exc_info.value) or "glob" in str(exc_info.value)


# --- New Tests for Overlapping Policies, Parameter Boundaries, & Fallback Mechanics ---


def test_validate_protected_paths_non_string_direct():
    """Directly call validate_protected_paths with a non-string element to cover line 51."""
    with pytest.raises(ValueError, match="Each protected path must be a string."):
        Settings.validate_protected_paths([123])


def test_validate_policies_not_dict_direct():
    """Test that a ValueError is raised if a policy is not a dict, calling the validator directly."""
    with pytest.raises(ValueError, match="Each policy must be a dictionary"):
        Settings.validate_policies([123])


def test_validate_policies_invalid_type():
    """Test that a ValueError is raised for invalid policy type."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            POLICIES=[
                {
                    "type": "invalid",
                    "expression": "abc",
                    "target_path": "valid",
                    "priority": 1,
                }
            ]
        )
    assert "Invalid policy type" in str(exc_info.value)


def test_validate_policies_invalid_expression():
    """Test that a ValueError is raised if policy expression is empty or not a string."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            POLICIES=[
                {
                    "type": "keyword",
                    "expression": 123,
                    "target_path": "valid",
                    "priority": 1,
                }
            ]
        )
    assert "Policy expression must be a non-empty string" in str(exc_info.value)

    with pytest.raises(ValidationError) as exc_info2:
        Settings(
            POLICIES=[
                {
                    "type": "keyword",
                    "expression": "   ",
                    "target_path": "valid",
                    "priority": 1,
                }
            ]
        )
    assert "Policy expression must be a non-empty string" in str(exc_info2.value)


def test_validate_policies_invalid_priority():
    """Test that a ValueError is raised if policy does not have an integer priority."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            POLICIES=[{"type": "keyword", "expression": "abc", "target_path": "valid"}]
        )
    assert "Policy must have an integer priority" in str(exc_info.value)

    with pytest.raises(ValidationError) as exc_info2:
        Settings(
            POLICIES=[
                {
                    "type": "keyword",
                    "expression": "abc",
                    "target_path": "valid",
                    "priority": "high",
                }
            ]
        )
    assert "Policy must have an integer priority" in str(exc_info2.value)


def test_policy_overlap_keyword_masks_keyword(caplog):
    """Test that rule overlap is detected when a keyword masks another keyword."""
    policies = [
        {
            "type": "keyword",
            "expression": "abc",
            "target_path": "path1",
            "priority": 10,
        },
        {
            "type": "keyword",
            "expression": "abcde",
            "target_path": "path2",
            "priority": 5,
        },
    ]
    with caplog.at_level(logging.WARNING):
        Settings(POLICIES=policies)
    assert "Rule overlap detected" in caplog.text
    assert "fully masked/shadowed by" in caplog.text


def test_policy_overlap_pattern_masks_pattern(caplog):
    """Test that rule overlap is detected when a pattern masks another pattern."""
    policies = [
        {
            "type": "pattern",
            "expression": "abc",
            "target_path": "path1",
            "priority": 10,
        },
        {
            "type": "pattern",
            "expression": "abcde",
            "target_path": "path2",
            "priority": 5,
        },
    ]
    with caplog.at_level(logging.WARNING):
        Settings(POLICIES=policies)
    assert "Rule overlap detected" in caplog.text


def test_policy_overlap_pattern_masks_override(caplog):
    """Test that rule overlap is detected when a pattern masks an override."""
    policies = [
        {
            "type": "pattern",
            "expression": "abc",
            "target_path": "path1",
            "priority": 10,
        },
        {
            "type": "override",
            "expression": "abcde",
            "target_path": "path2",
            "priority": 5,
        },
    ]
    with caplog.at_level(logging.WARNING):
        Settings(POLICIES=policies)
    assert "Rule overlap detected" in caplog.text


def test_policy_overlap_override_masks_override(caplog):
    """Test that rule overlap is detected when an override masks another override."""
    policies = [
        {
            "type": "override",
            "expression": "abc",
            "target_path": "path1",
            "priority": 10,
        },
        {
            "type": "override",
            "expression": "abcde",
            "target_path": "path2",
            "priority": 5,
        },
    ]
    with caplog.at_level(logging.WARNING):
        Settings(POLICIES=policies)
    assert "Rule overlap detected" in caplog.text


def test_policy_no_overlap_pattern_does_not_mask_keyword(caplog):
    """A higher priority pattern should NOT mask a lower priority keyword."""
    policies = [
        {
            "type": "pattern",
            "expression": "abc",
            "target_path": "path1",
            "priority": 10,
        },
        {
            "type": "keyword",
            "expression": "abcde",
            "target_path": "path2",
            "priority": 5,
        },
    ]
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        Settings(POLICIES=policies)
    assert "Rule overlap detected" not in caplog.text


def test_policy_no_overlap_override_does_not_mask_pattern(caplog):
    """A higher priority override should NOT mask a lower priority pattern."""
    policies = [
        {
            "type": "override",
            "expression": "abc",
            "target_path": "path1",
            "priority": 10,
        },
        {
            "type": "pattern",
            "expression": "abcde",
            "target_path": "path2",
            "priority": 5,
        },
    ]
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        Settings(POLICIES=policies)
    assert "Rule overlap detected" not in caplog.text


def test_policy_no_overlap_different_expressions(caplog):
    """Test that completely different expressions do not trigger any overlap warning (branch of is_masked_by where ha_expr is not in lo_expr)."""
    policies = [
        {
            "type": "keyword",
            "expression": "abc",
            "target_path": "path1",
            "priority": 10,
        },
        {"type": "keyword", "expression": "xyz", "target_path": "path2", "priority": 5},
    ]
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        Settings(POLICIES=policies)
    assert "Rule overlap detected" not in caplog.text


def test_policy_overlap_empty_expr_branch():
    """Test the branch in is_masked_by where an expression lower() is empty using a custom str subclass."""

    class TrickStr(str):
        def lower(self):
            return ""

    policies = [
        {
            "type": "keyword",
            "expression": TrickStr("abc"),
            "target_path": "path1",
            "priority": 10,
        },
        {
            "type": "keyword",
            "expression": TrickStr("abcde"),
            "target_path": "path2",
            "priority": 5,
        },
    ]
    settings = Settings(POLICIES=policies)
    assert len(settings.POLICIES) == 2


def test_app_settings_init_validation_error():
    """Test that AppSettings exits with code 1 if Settings initialization fails."""
    with (
        patch("sys.exit", side_effect=SystemExit) as mock_exit,
        patch.dict(os.environ, {"MAX_WORKERS": "invalid"}),
    ):
        with pytest.raises(SystemExit):
            AppSettings()
        mock_exit.assert_called_once_with(1)


def test_app_settings_load_no_schema_file(tmp_path):
    """Test AppSettings load when config_schema.json does not exist."""
    from pathlib import Path

    mock_filepath = tmp_path / "settings.json"
    mock_filepath.write_text(json.dumps({"MAX_WORKERS": 8}))

    original_exists = Path.exists

    def mock_exists(self):
        if "config_schema.json" in str(self):
            return False
        return original_exists(self)

    with patch.object(Path, "exists", mock_exists):
        app_settings = AppSettings(filepath=str(mock_filepath))
        assert app_settings.MAX_WORKERS == 8


def test_app_settings_load_invalid_root_schema(tmp_path, caplog):
    """Test AppSettings load with non-dict root to trigger schema validation error with empty path."""
    mock_filepath = tmp_path / "settings.json"
    mock_filepath.write_text(json.dumps([1, 2, 3]))

    with caplog.at_level(logging.WARNING):
        AppSettings(filepath=str(mock_filepath))
    assert "Configuration validation failed for field 'root'" in caplog.text


def test_app_settings_load_schema_and_pydantic_error(tmp_path, caplog):
    """Test AppSettings load with both schema mismatch and setattr failure to cover branch 253->257."""
    mock_filepath = tmp_path / "settings.json"
    mock_filepath.write_text(
        json.dumps({"UNKNOWN_KEY": "some_value", "MAX_WORKERS": "invalid"})
    )

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        AppSettings(filepath=str(mock_filepath))

    # UNKNOWN_KEY was skipped, MAX_WORKERS failed schema validation (logged).
    assert "Configuration validation failed for field 'MAX_WORKERS'" in caplog.text
    # Check that "Invalid MAX_WORKERS in config" was NOT logged to verify branch 253->257 is covered.
    assert "Invalid MAX_WORKERS in config" not in caplog.text


def test_app_settings_load_corrupt_json(tmp_path, caplog):
    """Test that loading a corrupt/invalid JSON file safely falls back to defaults."""
    mock_filepath = tmp_path / "settings.json"
    mock_filepath.write_text("{invalid json")

    with caplog.at_level(logging.WARNING):
        app_settings = AppSettings(filepath=str(mock_filepath))
    assert "Failed to load settings, using defaults:" in caplog.text
    assert app_settings._has_validation_errors is True


def test_app_settings_load_access_failure(tmp_path, caplog):
    """Test that loading settings with a PermissionError safely falls back to defaults."""
    mock_filepath = tmp_path / "settings.json"
    mock_filepath.write_text(json.dumps({"MAX_WORKERS": 8}))

    def mock_open_raise_permission(*args, **kwargs):
        raise PermissionError("Permission denied")

    with patch("builtins.open", mock_open_raise_permission):
        with caplog.at_level(logging.WARNING):
            app_settings = AppSettings(filepath=str(mock_filepath))
    assert "Failed to load settings, using defaults: Permission denied" in caplog.text
    assert app_settings._has_validation_errors is True


def test_app_settings_save_failure(tmp_path, caplog):
    """Test that a save failure logs an error message without crashing."""
    mock_filepath = tmp_path / "settings.json"
    app_settings = AppSettings(filepath=str(mock_filepath))

    # Mock open to raise permission error during save
    original_open = open

    def mock_open_write_fail(file, mode="r", *args, **kwargs):
        if file == str(mock_filepath) and "w" in mode:
            raise PermissionError("Write permission denied")
        return original_open(file, mode, *args, **kwargs)

    with patch("builtins.open", mock_open_write_fail):
        with caplog.at_level(logging.ERROR):
            app_settings._save()
    assert "Failed to save settings: Write permission denied" in caplog.text


def test_proxy_encryption_and_decryption_roundtrip(tmp_path):
    """Test that setting a proxy automatically encrypts it on disk but keeps it cleartext in memory."""
    mock_filepath = tmp_path / "settings.json"
    app_settings = AppSettings(filepath=str(mock_filepath))

    # Clean up background timers
    if app_settings._save_timer:
        app_settings._save_timer.cancel()

    proxy_string = "http" + "://user:password123@proxy.example.com:8080"
    app_settings.PROXY = proxy_string

    assert app_settings.PROXY == proxy_string

    # Trigger a synchronous save to inspect disk contents
    app_settings._save()

    # Read from disk to verify it's encrypted and not human readable (doesn't contain password123)
    with open(mock_filepath, "r", encoding="utf-8") as f:
        disk_data = json.load(f)

    assert "password123" not in disk_data["PROXY"]
    assert disk_data["PROXY"].startswith("enc:")

    # Load from disk with a new AppSettings instance to verify decryption
    new_app_settings = AppSettings(filepath=str(mock_filepath))
    assert new_app_settings.PROXY == proxy_string

    if app_settings._save_timer:
        app_settings._save_timer.cancel()
    if new_app_settings._save_timer:
        new_app_settings._save_timer.cancel()


def test_proxy_automatic_migration_from_plaintext(tmp_path):
    """Test that legacy plaintext proxy settings on disk are automatically migrated to encrypted format."""
    mock_filepath = tmp_path / "settings.json"
    proxy_string = "http" + "://legacy_user:legacy_password@proxy.example.com:3128"

    # Write legacy format (plaintext) to settings file
    legacy_data = {"PROXY": proxy_string, "MAX_WORKERS": 4}
    with open(mock_filepath, "w", encoding="utf-8") as f:
        json.dump(legacy_data, f, indent=4)

    # Initialize AppSettings. This should detect plaintext, load it, and trigger auto-migration
    app_settings = AppSettings(filepath=str(mock_filepath))
    assert app_settings.PROXY == proxy_string

    # Synchronously run save to complete migration
    app_settings._save()

    if app_settings._save_timer:
        app_settings._save_timer.cancel()

    # Verify that the file on disk is now encrypted
    with open(mock_filepath, "r", encoding="utf-8") as f:
        disk_data = json.load(f)

    assert proxy_string not in disk_data["PROXY"]
    assert disk_data["PROXY"].startswith("enc:")

    # Load again to verify we can decrypt the migrated format
    reloaded_settings = AppSettings(filepath=str(mock_filepath))
    assert reloaded_settings.PROXY == proxy_string

    if reloaded_settings._save_timer:
        reloaded_settings._save_timer.cancel()


def test_granular_acceleration_and_multi_lang_defaults():
    """Test that default values for the acceleration and language fields are correctly initialized."""
    settings = Settings()
    assert settings.OCR_GPU_ENABLED is False
    assert settings.AUDIO_GPU_ENABLED is False
    assert settings.OCR_LANGUAGES == "en"


def test_ocr_languages_validation():
    """Test validator for OCR_LANGUAGES field."""
    # Valid values
    assert Settings(OCR_LANGUAGES="en").OCR_LANGUAGES == "en"
    assert Settings(OCR_LANGUAGES="en,de").OCR_LANGUAGES == "en,de"
    assert Settings(OCR_LANGUAGES="en, de").OCR_LANGUAGES == "en, de"
    assert Settings(OCR_LANGUAGES="ch_sim,en").OCR_LANGUAGES == "ch_sim,en"

    # Invalid type
    with pytest.raises(ValidationError):
        Settings(OCR_LANGUAGES=123)

    # Empty string
    with pytest.raises(ValidationError):
        Settings(OCR_LANGUAGES="")

    # Missing parts or spaces only / empty codes
    with pytest.raises(ValidationError):
        Settings(OCR_LANGUAGES="en,,de")

    with pytest.raises(ValidationError):
        Settings(OCR_LANGUAGES="en, ")

    # Non-alphanumeric/underscore codes
    with pytest.raises(ValidationError):
        Settings(OCR_LANGUAGES="en,de-ch")


def test_proxy_decryption_failure_placeholder(tmp_path):
    """Test that decryption failure for proxy sets it to a placeholder and preserves the encrypted string on subsequent saves."""
    mock_filepath = tmp_path / "settings.json"

    # Let's create an invalid encrypted proxy in the config
    invalid_encrypted_data = {
        "PROXY": "enc:invalid_ciphertext_that_cannot_be_decrypted",
        "MAX_WORKERS": 4,
    }
    with open(mock_filepath, "w", encoding="utf-8") as f:
        json.dump(invalid_encrypted_data, f, indent=4)

    # Initialize AppSettings. It should fail to decrypt, set PROXY to "<DECRYPTION_FAILED>"
    app_settings = AppSettings(filepath=str(mock_filepath))

    # Cancel the save timer so we can manually save and test
    if app_settings._save_timer:
        app_settings._save_timer.cancel()

    assert app_settings.PROXY == "<DECRYPTION_FAILED>"

    # Now trigger self._save() and verify that the invalid encrypted ciphertext is NOT lost on disk
    app_settings._save()

    with open(mock_filepath, "r", encoding="utf-8") as f:
        disk_data = json.load(f)

    assert disk_data["PROXY"] == "enc:invalid_ciphertext_that_cannot_be_decrypted"

    # Now verify that the user can successfully overwrite the placeholder in memory/UI
    app_settings.PROXY = "http" + "://new-proxy:8080"
    assert app_settings.PROXY == "http" + "://new-proxy:8080"

    # Trigger save again, and check that the new proxy is encrypted
    app_settings._save()

    with open(mock_filepath, "r", encoding="utf-8") as f:
        disk_data = json.load(f)

    assert disk_data["PROXY"].startswith("enc:")
    assert "new-proxy" not in disk_data["PROXY"]

    if app_settings._save_timer:
        app_settings._save_timer.cancel()
