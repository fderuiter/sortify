import json
from unittest.mock import patch

from app.config import AppSettings


def test_revalidate_unlocks_auto_save_and_persists(tmp_path):
    """Test that correcting invalid settings and calling revalidate clears error flags, unlocks auto-saving, and saves to disk."""
    config_file = tmp_path / "settings.json"
    invalid_data = {"MAX_WORKERS": 999, "PROXY": "http://127.0.0.1:8080"}
    config_file.write_text(json.dumps(invalid_data), encoding="utf-8")

    settings = AppSettings(filepath=str(config_file))
    assert settings._has_validation_errors is True
    assert len(settings._validation_errors) > 0

    # Fixing the field via __setattr__ triggers revalidate automatically
    settings.MAX_WORKERS = 4

    assert settings._has_validation_errors is False
    assert settings._validation_errors == []

    if settings._save_timer:
        settings._save_timer.cancel()
    settings._save()

    saved_data = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved_data["MAX_WORKERS"] == 4
    assert saved_data["PROXY"].startswith("enc:")


def test_revalidate_returns_false_if_errors_remain(tmp_path):
    """Test that revalidate returns False and keeps saving locked if errors remain."""
    config_file = tmp_path / "settings.json"
    config_file.write_text(json.dumps({"MAX_WORKERS": 999}), encoding="utf-8")

    settings = AppSettings(filepath=str(config_file))
    # Inject validation error in _validation_errors
    settings._validation_errors = [{"field": "MAX_WORKERS", "message": "invalid"}]
    settings._has_validation_errors = True

    with patch("app.config.Settings", side_effect=ValueError("Invalid worker count")):
        res = settings.revalidate()
        assert res is False
        assert settings._has_validation_errors is True

    if settings._save_timer:
        settings._save_timer.cancel()
