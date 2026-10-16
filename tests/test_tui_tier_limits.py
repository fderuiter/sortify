"""Tests for tier limit capacity banner and contextual upgrade prompts in TUI."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.config import AppSettings
from app.ui.tui import AutoSorterTUI, LicenseActivationModal, TierLimitModal


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


def test_tier_limit_modal_a11y_compliance():
    """Verify WCAG 2.1 accessibility compliance for TierLimitModal."""
    modal = TierLimitModal(current_count=12, max_folders=12)
    audit = modal.audit_a11y_compliance()
    assert audit["compliant"] is True, f"A11y violations: {audit['violations']}"


def test_license_activation_modal_a11y_compliance():
    """Verify WCAG 2.1 accessibility compliance for LicenseActivationModal."""
    modal = LicenseActivationModal()
    audit = modal.audit_a11y_compliance()
    assert audit["compliant"] is True, f"A11y violations: {audit['violations']}"


@pytest.mark.slow
def test_exceeding_limits_triggers_tier_limit_modal_and_upgrade_button():
    """Verify attempting to create a folder at max limit triggers TierLimitModal and opens LicenseActivationModal."""

    async def _test():
        settings = AppSettings()
        settings.AI_CONSENT_GRANTED = True
        settings.MAX_FOLDERS = 4
        app = AutoSorterTUI(settings=settings)

        # Set 4 existing folders (limit reached)
        app.plan = {
            "Folder1": {},
            "Folder2": {},
            "Folder3": {},
            "Folder4": {},
        }

        async with app.run_test(size=(100, 30)) as pilot:
            app.rebuild_tree()
            assert app.sub_title == "4 / 4 Subdirectories used"

            # Attempt to create new folder at capacity
            app.action_new_folder()
            await pilot.pause()

            # Verify TierLimitModal was pushed
            assert isinstance(app.screen, TierLimitModal)
            tier_modal = app.screen
            assert tier_modal.current_count == 4
            assert tier_modal.max_folders == 4

            # Press upgrade button on callout
            await pilot.click("#btn-upgrade")
            await pilot.pause()

            # Verify LicenseActivationModal was launched from TierLimitModal button
            assert isinstance(app.screen, LicenseActivationModal)
            license_modal = app.screen

            # Enter license key and submit
            await pilot.click("#input-license-key")
            await pilot.press(*"PRO-12345678-KEY")
            await pilot.click("#btn-activate")
            await pilot.pause()

            # Verify license activation updated Pro settings and expanded capacity limit
            assert app.settings.IS_PRO_TIER is True
            assert app.settings.LICENSE_KEY == "PRO-12345678-KEY"
            assert app.settings.MAX_FOLDERS == 50
            assert app.sub_title == "4 / 50 Subdirectories used"

    asyncio.run(_test())
