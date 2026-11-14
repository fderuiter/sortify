"""Unit and TUI integration tests for ExportRulesModal dialog."""

import asyncio
from unittest.mock import patch

from textual.widgets import Button, Input, Label, Select

from app.config import AppSettings
from app.ui.modals.rules import ExportRulesModal, TemplateGalleryModal
from app.ui.tui import AutoSorterTUI


def test_export_rules_modal_initial_state(tmp_path):
    """Verify ExportRulesModal instantiates with correct default widgets and counts."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        settings.AI_CONSENT_GRANTED = True
        settings.KEYWORD_RULES = {"invoice": "Financial/Invoices"}
        settings.POLICIES = [
            {
                "type": "keyword",
                "expression": "confidential tax",
                "target_path": "Taxes/Confidential",
                "priority": 90,
            }
        ]

        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            modal = ExportRulesModal(settings)
            app.push_screen(modal)
            await pilot.pause(0.1)

            assert modal.settings is settings
            assert modal.query_one("#modal-title") is not None
            assert modal.query_one("#select-export-format", Select) is not None
            assert modal.query_one("#input-export-path", Input) is not None
            assert modal.query_one("#btn-do-export", Button) is not None
            assert modal.query_one("#btn-cancel-export", Button) is not None

    asyncio.run(_test())


def test_export_rules_modal_tui_interactions(tmp_path):
    """Verify ExportRulesModal format switching, validation, export execution, and announcements."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        settings.AI_CONSENT_GRANTED = True
        settings.KEYWORD_RULES = {
            "invoice": "Financial/Invoices",
            "receipt": "Financial/Receipts",
        }
        settings.POLICIES = [
            {
                "type": "keyword",
                "expression": "confidential tax",
                "target_path": "Taxes/Confidential",
                "priority": 90,
                "halting": False,
            }
        ]

        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        async with app.run_test() as pilot:
            await pilot.pause(0.1)

            export_modal = ExportRulesModal(settings)
            app.push_screen(export_modal)
            await pilot.pause(0.1)

            assert isinstance(app.screen, ExportRulesModal)

            # Check mount announcement
            announcements = [a["message"] for a in export_modal.announcements]
            assert any("Opened export rules modal" in m for m in announcements)

            # Check initial format and destination
            sel = export_modal.query_one("#select-export-format", Select)
            assert sel.value == "sortify_json"

            inp = export_modal.query_one("#input-export-path", Input)
            assert inp.value.endswith(".json")

            # Switch format to Hazel
            sel.value = "hazel"
            await pilot.pause(0.1)
            assert inp.value.endswith(".hazelrules")
            announcements = [a["message"] for a in export_modal.announcements]
            assert any("Hazel" in m for m in announcements)

            # Switch format to DropIt
            sel.value = "dropit_ini"
            await pilot.pause(0.1)
            assert inp.value.endswith(".ini")
            announcements = [a["message"] for a in export_modal.announcements]
            assert any("DropIt" in m for m in announcements)

            # Switch back to Sortify JSON
            sel.value = "sortify_json"
            await pilot.pause(0.1)
            assert inp.value.endswith(".json")

            # 1. Validation error: empty destination path
            inp.value = ""
            with patch.object(app, "notify") as mock_notify:
                export_modal.action_export()
                await pilot.pause(0.1)

                lbl_status = export_modal.query_one("#lbl-export-status", Label)
                assert "Please provide a valid destination" in str(lbl_status.render())
                mock_notify.assert_called_with(
                    "Please provide a valid destination file path.", severity="error"
                )

            # 2. Validation error: directory traversal
            inp.value = "../dangerous/rules.json"
            with patch.object(app, "notify") as mock_notify:
                export_modal.action_export()
                await pilot.pause(0.1)

                lbl_status = export_modal.query_one("#lbl-export-status", Label)
                assert "Directory traversal" in str(lbl_status.render())
                assert mock_notify.call_args[1].get("severity") == "error"

            # 3. Successful export
            valid_dest = tmp_path / "my_exported_rules.json"
            inp.value = str(valid_dest)

            with patch.object(app, "notify") as mock_notify:
                export_modal.action_export()
                await pilot.pause(0.1)

                assert valid_dest.exists()
                mock_notify.assert_called_once()
                assert (
                    "Successfully exported active rules" in mock_notify.call_args[0][0]
                )

    asyncio.run(_test())


def test_export_rules_modal_cancel_and_escape(tmp_path):
    """Verify cancelling or pressing Escape dismisses ExportRulesModal."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        settings.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

        async with app.run_test() as pilot:
            await pilot.pause(0.1)

            # Test cancel button
            modal1 = ExportRulesModal(settings)
            app.push_screen(modal1)
            await pilot.pause(0.1)
            assert isinstance(app.screen, ExportRulesModal)

            modal1.action_close()
            await pilot.pause(0.1)
            assert not isinstance(app.screen, ExportRulesModal)

            # Test escape key
            modal2 = ExportRulesModal(settings)
            app.push_screen(modal2)
            await pilot.pause(0.1)
            assert isinstance(app.screen, ExportRulesModal)

            await pilot.press("escape")
            await pilot.pause(0.1)
            assert not isinstance(app.screen, ExportRulesModal)

    asyncio.run(_test())


def test_template_gallery_launches_export_rules_modal(tmp_path):
    """Verify TemplateGalleryModal contains Export Rules button that opens ExportRulesModal."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        settings.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            gallery_modal = TemplateGalleryModal(settings)
            app.push_screen(gallery_modal)
            await pilot.pause(0.1)

            assert isinstance(app.screen, TemplateGalleryModal)

            btn_export = gallery_modal.query_one("#btn-export-rules", Button)
            assert btn_export is not None
            assert "Export" in str(btn_export.label)

            # Click or press export button
            gallery_modal.action_export_rules()
            await pilot.pause(0.1)

            assert isinstance(app.screen, ExportRulesModal)

    asyncio.run(_test())
