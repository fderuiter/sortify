"""TUI integration tests for onboarding starter template gallery and settings integration."""

import asyncio
import tempfile

from app.config import AppSettings
from app.ui.tui import AutoSorterTUI, SettingsModal, TemplateGalleryModal, WizardModal


def test_wizard_modal_starter_templates_applied(tmp_path):
    """Verify selecting starter rule template packs during wizard onboarding populates settings."""

    async def _test():
        with tempfile.NamedTemporaryFile(suffix=".json") as f:
            settings = AppSettings(filepath=f.name)
            settings._settings_model.AI_CONSENT_GRANTED = None
            app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

            async with app.run_test() as pilot:
                await pilot.pause(0.2)
                modal = app.screen
                assert isinstance(modal, WizardModal)

                # Finish wizard onboarding
                await pilot.pause(0.1)
                modal.action_finish()
                await pilot.pause(0.1)

                # Verify default starter templates (financial_tax, personal_admin) populated KEYWORD_RULES & POLICIES
                assert "invoice" in settings.KEYWORD_RULES
                assert "utility" in settings.KEYWORD_RULES
                assert len(settings.POLICIES) > 0

    asyncio.run(_test())


def test_template_gallery_modal_apply_packs(tmp_path):
    """Verify post-onboarding template pack selection from TemplateGalleryModal populates settings."""

    async def _test():
        with tempfile.NamedTemporaryFile(suffix=".json") as f:
            settings = AppSettings(filepath=f.name)
            settings.AI_CONSENT_GRANTED = True
            app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

            async with app.run_test() as pilot:
                await pilot.pause(0.1)
                gallery_modal = TemplateGalleryModal(settings)
                app.push_screen(gallery_modal)
                await pilot.pause(0.1)

                assert isinstance(app.screen, TemplateGalleryModal)

                # Check legal contracts and medical health template checkboxes
                gallery_modal.query_one("#chk-gallery-legal_contracts").value = True
                gallery_modal.query_one("#chk-gallery-medical_health").value = True

                # Apply selected templates
                gallery_modal.action_apply_templates()
                await pilot.pause(0.1)

                # Verify rules populated safely
                assert "contract" in settings.KEYWORD_RULES
                assert "prescription" in settings.KEYWORD_RULES
                assert any(
                    "insurance claim" in p.get("expression", "")
                    for p in settings.POLICIES
                )

    asyncio.run(_test())


def test_settings_modal_launches_template_gallery(tmp_path):
    """Verify SettingsModal provides access to TemplateGalleryModal via button action."""

    async def _test():
        with tempfile.NamedTemporaryFile(suffix=".json") as f:
            settings = AppSettings(filepath=f.name)
            settings.AI_CONSENT_GRANTED = True
            app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

            async with app.run_test() as pilot:
                await pilot.pause(0.1)
                settings_modal = SettingsModal(settings)
                app.push_screen(settings_modal)
                await pilot.pause(0.1)

                assert isinstance(app.screen, SettingsModal)

                # Trigger open template gallery button action
                settings_modal.action_open_templates()
                await pilot.pause(0.1)

                # Verify TemplateGalleryModal is displayed
                assert isinstance(app.screen, TemplateGalleryModal)

    asyncio.run(_test())
