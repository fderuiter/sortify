"""Integration tests for LicenseModal and TUI license activation workflow."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from textual.widgets import Input, Label

from app.config import AppSettings
from app.core.license import generate_license_key
from app.ui.tui import AutoSorterTUI, LicenseModal, SettingsModal

pytestmark = pytest.mark.xdist_group(name="tui")


@pytest.fixture(autouse=True)
def isolated_app_dir(monkeypatch, tmp_path):
    """Ensure AppSettings is isolated from persistent disk configuration changes."""
    import app.config
    import app.core.session

    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path))
    monkeypatch.setattr(app.config, "get_app_dir", lambda: tmp_path)
    monkeypatch.setattr(
        app.config.AppSettings, "_trigger_save", lambda self: self._save()
    )
    monkeypatch.setattr(
        app.core.session,
        "scan_abandoned_sessions_async",
        AsyncMock(return_value=[]),
    )


def test_license_modal_activation_valid_key(tmp_path):
    """Test activating a valid Pro license key via LicenseModal."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        assert settings.LICENSE_TIER == "Community"
        assert settings.MAX_FOLDERS == 12

        valid_key = generate_license_key(owner="pro_user@domain.com", tier="Pro")

        app = AutoSorterTUI(settings=settings, skip_wizard=True)
        async with app.run_test() as pilot:
            await pilot.press("ctrl+l")
            await pilot.pause(0.05)
            assert isinstance(app.screen, LicenseModal)

            modal = app.screen
            inp = modal.query_one("#input-license-key", Input)
            inp.value = valid_key

            await pilot.click("#btn-activate")
            await pilot.pause(0.05)

            assert not isinstance(app.screen, LicenseModal)
            assert app.settings.LICENSE_TIER == "Pro"
            assert app.settings.LICENSE_OWNER == "pro_user@domain.com"
            assert app.settings.MAX_FOLDERS == 50

    asyncio.run(_test())


def test_license_modal_activation_invalid_key(tmp_path):
    """Test entering an invalid license key displays an error and preserves tier state."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        assert settings.LICENSE_TIER == "Community"

        app = AutoSorterTUI(settings=settings, skip_wizard=True)
        async with app.run_test() as pilot:
            await pilot.press("ctrl+l")
            await pilot.pause(0.05)
            assert isinstance(app.screen, LicenseModal)

            modal = app.screen
            inp = modal.query_one("#input-license-key", Input)
            inp.value = "INVALID-KEY-STRING"

            await pilot.click("#btn-activate")
            await pilot.pause(0.05)

            # Modal remains open, non-blocking error label displayed, tier preserved
            assert isinstance(app.screen, LicenseModal)
            err_lbl = modal.query_one("#license-status-msg", Label)
            assert "Error" in str(err_lbl.content) or "format" in str(err_lbl.content)
            assert app.settings.LICENSE_TIER == "Community"

    asyncio.run(_test())


def test_license_modal_deactivation(tmp_path):
    """Test deactivating an active Pro license key."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        valid_key = generate_license_key(owner="test@test.com", tier="Pro")
        settings.LICENSE_KEY = valid_key
        settings.LICENSE_TIER = "Pro"
        settings.LICENSE_OWNER = "test@test.com"

        app = AutoSorterTUI(settings=settings, skip_wizard=True)
        async with app.run_test() as pilot:
            await pilot.press("ctrl+l")
            await pilot.pause(0.05)
            assert isinstance(app.screen, LicenseModal)

            modal = app.screen
            await pilot.click("#btn-deactivate")
            await pilot.pause(0.05)

            assert app.settings.LICENSE_TIER == "Community"
            assert app.settings.LICENSE_KEY == ""

    asyncio.run(_test())


def test_settings_modal_license_button(tmp_path):
    """Test opening LicenseModal from SettingsModal."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))

        app = AutoSorterTUI(settings=settings, skip_wizard=True)
        async with app.run_test() as pilot:
            await pilot.press("ctrl+o")
            await pilot.pause(0.05)
            assert isinstance(app.screen, SettingsModal)

            btn = app.screen.query_one("#btn-open-license")
            btn.focus()
            await pilot.press("enter")
            await pilot.pause(0.05)
            assert isinstance(app.screen, LicenseModal)

    asyncio.run(_test())
