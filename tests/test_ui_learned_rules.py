"""Tests for Learned Rules Settings."""

from app.config import AppSettings


def test_learned_rules_settings_assignment(tmp_path):
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    assert settings.KEYWORD_RULES == {}

    settings.KEYWORD_RULES = {"invoice": "Invoices", "receipt": "Receipts"}
    assert settings.KEYWORD_RULES["invoice"] == "Invoices"

    if settings._save_timer:
        settings._save_timer.cancel()
