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


def test_wizard_modal_switch_and_checkbox_announcements(tmp_path):
    """Verify toggling switches and checkboxes in WizardModal emits screen reader announcements."""

    async def _test():
        with tempfile.NamedTemporaryFile(suffix=".json") as f:
            settings = AppSettings(filepath=f.name)
            settings._settings_model.AI_CONSENT_GRANTED = None
            app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

            async with app.run_test() as pilot:
                await pilot.pause(0.2)
                modal = app.screen
                assert isinstance(modal, WizardModal)

                from textual.widgets import Checkbox, Switch

                # Toggle AI Consent switch
                sw_consent = modal.query_one("#switch-consent", Switch)
                sw_consent.value = True
                await pilot.pause(0.05)
                sw_consent.value = False
                await pilot.pause(0.05)

                # Toggle Sample Corpus switch
                sw_sample = modal.query_one("#switch-sample-corpus", Switch)
                sw_sample.value = True
                await pilot.pause(0.05)
                sw_sample.value = False
                await pilot.pause(0.05)

                # Toggle Template Checkbox
                chk_med = modal.query_one("#chk-template-medical_health", Checkbox)
                chk_med.value = True
                await pilot.pause(0.05)
                chk_med.value = False
                await pilot.pause(0.05)

                messages = [a["message"] for a in modal.announcements]
                assert "AI consent enabled." in messages
                assert "AI consent disabled." in messages
                assert "Sample document corpus generation enabled." in messages
                assert "Sample document corpus generation disabled." in messages
                assert "Medical & Health Records template pack selected." in messages
                assert "Medical & Health Records template pack deselected." in messages

    asyncio.run(_test())


def test_template_gallery_modal_checkbox_announcements(tmp_path):
    """Verify toggling checkboxes in TemplateGalleryModal emits screen reader announcements."""

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

                from textual.widgets import Checkbox

                chk_legal = gallery_modal.query_one(
                    "#chk-gallery-legal_contracts", Checkbox
                )
                chk_legal.value = True
                await pilot.pause(0.05)
                chk_legal.value = False
                await pilot.pause(0.05)

                messages = [a["message"] for a in gallery_modal.announcements]
                assert "Legal & Contracts template pack selected." in messages
                assert "Legal & Contracts template pack deselected." in messages

    asyncio.run(_test())


def test_modal_unrecognized_controls_handled_gracefully(tmp_path):
    """Verify unrecognized switch or checkbox IDs do not emit empty announcements or crash."""

    async def _test():
        with tempfile.NamedTemporaryFile(suffix=".json") as f:
            settings = AppSettings(filepath=f.name)
            settings._settings_model.AI_CONSENT_GRANTED = None
            app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

            async with app.run_test() as pilot:
                await pilot.pause(0.2)
                modal = app.screen
                assert isinstance(modal, WizardModal)

                from textual.widgets import Checkbox, Switch

                dummy_sw = Switch(id="switch-unknown")
                sw_event = Switch.Changed(dummy_sw, True)
                modal.on_switch_changed(sw_event)

                dummy_chk = Checkbox(id="chk-unknown")
                chk_event = Checkbox.Changed(dummy_chk, True)
                modal.on_checkbox_changed(chk_event)

                messages = [a["message"] for a in modal.announcements]
                assert not any("unknown" in m for m in messages)
                assert not any(m == "" for m in messages)

    asyncio.run(_test())

