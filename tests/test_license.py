"""Unit tests for offline license validation and cryptographic key engine."""

from app.config import AppSettings
from app.core.license import (
    generate_license_key,
    mask_license_key,
    validate_license_key,
)
from app.core.security import (
    mask_license_key as sec_mask_license_key,
)
from app.core.security import (
    validate_license_key as sec_validate_license_key,
)


def test_license_key_masking():
    """Test license key string masking helper."""
    assert mask_license_key("") == ""
    assert mask_license_key(None) == ""
    assert mask_license_key("123") == "****"
    
    key = "SORTIFY-PRO-dGVzdHBheWxvYWQ.c2lnbmF0dXJl"
    masked = mask_license_key(key)
    assert masked.startswith("SORTIFY-PRO-")
    assert "dGVzdHBheWxvYWQ" not in masked
    assert "****" in masked

    # Security module re-export parity check
    assert sec_mask_license_key(key) == masked


def test_valid_license_generation_and_validation():
    """Test generating a valid Pro license key and verifying it offline."""
    key = generate_license_key(owner="test@sortify.app", tier="Pro", expires="2099-12-31")
    assert key.startswith("SORTIFY-PRO-")

    val_res = validate_license_key(key)
    assert val_res["valid"] is True
    assert val_res["tier"] == "Pro"
    assert val_res["owner"] == "test@sortify.app"
    assert val_res["expires"] == "2099-12-31"
    assert "Successfully validated" in val_res["message"]

    # Security module re-exports
    sec_val = sec_validate_license_key(key)
    assert sec_val["valid"] is True


def test_invalid_and_tampered_license_keys():
    """Test handling of empty, malformed, and signature-tampered license keys."""
    # Empty
    res_empty = validate_license_key("")
    assert res_empty["valid"] is False
    assert res_empty["tier"] == "Community"

    # Malformed
    res_malformed = validate_license_key("INVALID-KEY-STRING")
    assert res_malformed["valid"] is False
    assert "format" in res_malformed["message"].lower()

    # Tampered signature
    valid_key = generate_license_key(owner="user@domain.com", tier="Pro")
    parts = valid_key.split(".")
    tampered_sig = parts[1][:-2] + ("00" if not parts[1].endswith("00") else "11")
    tampered_key = f"{parts[0]}.{tampered_sig}"

    res_tampered = validate_license_key(tampered_key)
    assert res_tampered["valid"] is False
    assert "signature" in res_tampered["message"].lower() or "invalid" in res_tampered["message"].lower()


def test_expired_license_key():
    """Test handling of expired license keys."""
    expired_key = generate_license_key(owner="expired@user.com", tier="Pro", expires="2020-01-01")
    res = validate_license_key(expired_key)
    assert res["valid"] is False
    assert res["tier"] == "Community"
    assert "expired" in res["message"].lower()


def test_app_settings_license_persistence(tmp_path):
    """Test persistence and loading of license keys in AppSettings."""
    settings_file = tmp_path / "settings.json"
    
    app_settings = AppSettings(filepath=str(settings_file))
    assert app_settings.LICENSE_TIER == "Community"
    assert app_settings.LICENSE_KEY == ""

    # Generate and assign valid key
    valid_key = generate_license_key(owner="corporate@corp.com", tier="Pro")
    val_res = validate_license_key(valid_key)
    assert val_res["valid"] is True

    app_settings.LICENSE_KEY = valid_key
    app_settings.LICENSE_TIER = val_res["tier"]
    app_settings.LICENSE_OWNER = val_res["owner"]
    app_settings.LICENSE_EXPIRATION = val_res["expires"]
    app_settings.MAX_FOLDERS = 50

    # Force save
    app_settings._save()

    # Reload settings from disk
    reloaded_settings = AppSettings(filepath=str(settings_file))
    assert reloaded_settings.LICENSE_KEY == valid_key
    assert reloaded_settings.LICENSE_TIER == "Pro"
    assert reloaded_settings.LICENSE_OWNER == "corporate@corp.com"
    assert reloaded_settings.MAX_FOLDERS == 50


def test_app_settings_invalid_license_key_load_fallback(tmp_path):
    """Test that invalid license key on load falls back to Community tier without corrupting file."""
    settings_file = tmp_path / "settings.json"
    
    # Save a file with invalid key
    with open(settings_file, "w", encoding="utf-8") as f:
        f.write('{"LICENSE_KEY": "SORTIFY-PRO-invalid.signature", "MAX_FOLDERS": 12}')

    app_settings = AppSettings(filepath=str(settings_file))
    assert app_settings.LICENSE_TIER == "Community"
    assert app_settings.LICENSE_KEY == "SORTIFY-PRO-invalid.signature"
