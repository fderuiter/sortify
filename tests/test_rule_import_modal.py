"""Unit tests for RuleImportModal interactive preview and rule merging dialog."""

import json
import plistlib
import tempfile
from pathlib import Path

from textual.widgets import Button

from app.config import AppSettings
from app.core.rule_importers import get_rule_importer
from app.ui.tui import RuleImportModal


def test_rule_import_modal_instantiation(tmp_path):
    """Verify RuleImportModal instantiates and mounts correctly."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    modal = RuleImportModal(settings)
    assert modal.settings is settings
    assert modal.current_result is None


def test_rule_import_modal_parsing_and_merging(tmp_path):
    """Verify RuleImportModal parses a file and merges rules into settings."""
    hazel_data = {
        "Rules": [
            {
                "NSRuleName": "Financial Invoices",
                "NSRuleConditions": [
                    {
                        "target": "Name",
                        "operator": "contains",
                        "value": "invoice",
                    }
                ],
                "NSRuleActions": [
                    {
                        "DestinationPath": "Financial/Invoices",
                    }
                ],
            }
        ]
    }

    with tempfile.NamedTemporaryFile(
        suffix=".hazelrules", mode="wb", delete=False
    ) as f:
        plistlib.dump(hazel_data, f)
        temp_path = f.name

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    modal = RuleImportModal(settings)

    try:
        # Simulate user setting input path and triggering parse
        from app.core.rule_importers import HazelRuleImporter

        importer = HazelRuleImporter()
        res = importer.parse_file(temp_path)
        modal.current_result = res

        assert "invoice" in modal.current_result.keyword_rules

        # Perform merge action (new rules are staged disabled by default)
        modal.action_merge_rules()

        assert settings.DISABLED_KEYWORD_RULES.get("invoice") == "Financial/Invoices"
        assert "invoice" not in settings.KEYWORD_RULES
        pdf_policies = [p for p in settings.POLICIES if p.get("expression") == "*.pdf"]
        for pol in pdf_policies:
            assert pol.get("enabled") is False
    finally:
        Path(temp_path).unlink(missing_ok=True)


def test_rule_import_modal_conflict_preview_and_resolution(tmp_path):
    """Verify RuleImportModal displays conflicts and supports interactive resolution selection."""

    from app.core.rule_importers import ImportResult, RuleConflictAnalyzer

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.KEYWORD_RULES = {"invoice": "Existing/Invoices"}
    settings.POLICIES = [
        {
            "type": "pattern",
            "expression": "*.pdf",
            "target_path": "Existing/PDFs",
            "priority": 90,
        }
    ]

    modal = RuleImportModal(settings)

    res = ImportResult(
        keyword_rules={"invoice": "Imported/Invoices"},
        policies=[
            {
                "type": "pattern",
                "expression": "*.pdf",
                "target_path": "Imported/PDFs",
                "priority": 75,
            }
        ],
        source_format="Hazel",
    )

    modal.current_result = res
    modal.conflicts = RuleConflictAnalyzer.analyze(res, settings)
    modal.conflict_resolutions = {c.identifier: c.resolution for c in modal.conflicts}

    assert len(modal.conflicts) == 2

    # 1. Test bulk resolution strategy change
    class DummySelectEvent:
        value = "overwrite"

    modal._on_bulk_strategy_changed(DummySelectEvent())
    assert modal.conflict_resolutions["invoice"] == "overwrite"
    assert modal.conflict_resolutions["*.pdf (pattern)"] == "overwrite"

    # 2. Test individual conflict selection row toggle
    class DummyRowEvent:
        row_key = type("RowKey", (), {"value": "invoice"})()

    # Toggling 'invoice' row should cycle overwrite -> skip_conflicting
    modal._on_conflict_selected(DummyRowEvent())
    assert modal.conflict_resolutions["invoice"] == "skip_conflicting"
    assert modal.conflict_resolutions["*.pdf (pattern)"] == "overwrite"

    # 3. Perform merge with chosen resolutions
    modal.action_merge_rules()

    # invoice was set to skip_conflicting -> keeps Existing/Invoices
    assert settings.KEYWORD_RULES["invoice"] == "Existing/Invoices"
    # *.pdf policy was set to overwrite -> updated to Imported/PDFs
    pdf_policy = next(p for p in settings.POLICIES if p["expression"] == "*.pdf")
    assert pdf_policy["target_path"] == "Imported/PDFs"


def test_rule_import_modal_tagspaces_parsing_and_merging(tmp_path):
    """Verify RuleImportModal handles TagSpaces taggroup.json files and merges rules into settings."""
    ts_data = {
        "tagGroups": [
            {
                "title": "Medical",
                "tags": [
                    "prescription",
                    {"title": "lab report"},
                ],
            }
        ]
    }

    with tempfile.NamedTemporaryFile(
        suffix="taggroup.json", mode="w", encoding="utf-8", delete=False
    ) as f:
        json.dump(ts_data, f)
        temp_path = f.name

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    modal = RuleImportModal(settings)

    try:
        importer = get_rule_importer(temp_path)
        res = importer.parse_file(temp_path)
        modal.current_result = res

        assert "prescription" in modal.current_result.keyword_rules
        assert modal.current_result.keyword_rules["prescription"] == "Medical"

        modal.action_merge_rules()

        assert settings.DISABLED_KEYWORD_RULES.get("prescription") == "Medical"
        assert "prescription" not in settings.KEYWORD_RULES
        lab_policies = [
            p for p in settings.POLICIES if p.get("expression") == "lab report"
        ]
        assert len(lab_policies) == 1
        assert lab_policies[0].get("target_path") == "Medical"
        assert lab_policies[0].get("enabled") is False
    finally:
        Path(temp_path).unlink(missing_ok=True)


def test_rule_import_modal_save_failure_handling(mocker):
    """Verify RuleImportModal catches save exceptions, notifies error, announces error, and keeps modal open."""
    from app.core.rule_importers import ImportResult

    class MockFailingSettings:
        KEYWORD_RULES = {}
        POLICIES = []

        def _save(self):
            raise OSError("ReadOnlyFileSystem: cannot write settings.json")

    settings = MockFailingSettings()
    modal = RuleImportModal(settings)
    modal.current_result = ImportResult(keyword_rules={"invoice": "Invoices"})

    dismiss_mock = mocker.patch.object(modal, "dismiss")
    announce_mock = mocker.patch.object(modal, "announce")
    notify_mock = mocker.patch("app.ui.notifications.notify")

    modal.action_merge_rules()

    # Modal should NOT have been dismissed on save error
    dismiss_mock.assert_not_called()

    # Error should have been announced
    announce_mock.assert_called_once()
    announced_text = announce_mock.call_args[0][0]
    assert "Failed to save imported rules" in announced_text
    assert "ReadOnlyFileSystem" in announced_text

    # Error notification should have been issued
    notify_mock.assert_called_once()
    _, kwargs = notify_mock.call_args
    assert kwargs.get("type") == "error"


def test_rule_import_modal_dismissal(tmp_path, mocker):
    """Verify RuleImportModal dismisses on action_close and action_cancel delegates to action_close."""
    import asyncio

    from app.ui.tui import AutoSorterTUI

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    modal = RuleImportModal(settings)

    close_spy = mocker.spy(modal, "action_close")
    dismiss_mock = mocker.patch.object(modal, "dismiss")
    announce_mock = mocker.patch.object(modal, "announce")

    modal.action_cancel()

    close_spy.assert_called_once()
    dismiss_mock.assert_called_once_with(None)
    announce_mock.assert_called_once_with("Closed external rule importer modal.")

    # TUI test with close button and escape key
    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        async with app.run_test() as pilot:
            await pilot.pause(0.1)

            # Test close button
            modal1 = RuleImportModal(settings)
            app.push_screen(modal1)
            await pilot.pause(0.1)
            assert isinstance(app.screen, RuleImportModal)

            modal1.query_one("#btn-close-importer", Button).press()
            await pilot.pause(0.1)
            assert not isinstance(app.screen, RuleImportModal)

            # Test escape key
            modal2 = RuleImportModal(settings)
            app.push_screen(modal2)
            await pilot.pause(0.1)
            assert isinstance(app.screen, RuleImportModal)

            await pilot.press("escape")
            await pilot.pause(0.1)
            assert not isinstance(app.screen, RuleImportModal)

    asyncio.run(_test())


def test_rule_import_modal_browse_ini_preview_and_enable_toggle(tmp_path, mocker):
    """Verify Browse..., .ini format support, unarchive badge, warnings, and enable toggle."""
    import asyncio

    from textual.widgets import Checkbox, DataTable, Input, Label

    from app.core.native_dialog import NativeDialogError
    from app.ui.tui import AutoSorterTUI

    ini_file = tmp_path / "dropit_profile.ini"
    ini_file.write_text(
        "[DropIt]\n"
        "Profile = Imported INI\n\n"
        "[Association_1]\n"
        "Name = Unpack Bundles\n"
        "Rules = *.zip\n"
        "Action = Extract\n"
        "Destination = C:\\Users\\Alice\\Archives\\Unpacked\n\n"
        "[Association_2]\n"
        "Name = Copy Backups\n"
        "Rules = *.bak\n"
        "Action = Copy\n"
        "Destination = C:\\Users\\Alice\\Backups\n\n"
        "[Association_3]\n"
        "Name = Tax Receipts\n"
        "Rules = receipt\n"
        "Action = Move\n"
        "Destination = /Users/alice/Documents/Receipts\n",
        encoding="utf-8",
    )

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause(0.1)
            modal = RuleImportModal(settings)
            app.push_screen(modal)
            await pilot.pause(0.1)

            # Verify .ini and Browse... are exposed and disabled-by-default checkbox is checked
            inp = modal.query_one("#input-rule-file-path", Input)
            assert "ini" in inp.placeholder.lower()
            btn_browse = modal.query_one("#btn-browse-rule-file", Button)
            assert "Browse" in str(btn_browse.label)
            chk_disabled = modal.query_one("#chk-import-disabled", Checkbox)
            assert chk_disabled.value is True
            assert modal.import_disabled is True

            # 1. Test graceful fallback when native file dialog fails
            mocker.patch(
                "app.core.native_dialog.select_native_file",
                side_effect=NativeDialogError("headless display"),
            )
            btn_browse.press()
            await pilot.pause(0.2)
            status_lbl = modal.query_one("#lbl-import-status", Label)
            assert "Native file picker unavailable" in str(status_lbl.render())

            # 2. Test native file selection populating input and auto-parsing .ini preview
            mocker.patch(
                "app.core.native_dialog.select_native_file",
                return_value=str(ini_file),
            )
            btn_browse.press()
            await pilot.pause(0.25)

            assert inp.value == str(ini_file)
            assert modal.current_result is not None
            assert (
                modal.current_result.keyword_rules.get("receipt")
                == "Documents/Receipts"
            )

            dt_pol = modal.query_one("#dt-imported-policies", DataTable)
            assert dt_pol.row_count == 1
            first_row = dt_pol.get_row_at(0)
            assert "unarchive" in str(first_row[0]).lower()
            assert first_row[1] == "*.zip"
            assert first_row[2] == "Archives/Unpacked"

            # Warnings should report both converted absolute paths and skipped Copy action
            warn_labels = [
                str(lbl.render())
                for lbl in modal.query("#container-import-warnings Label")
            ]
            joined_warnings = "\n".join(warn_labels)
            assert "converted destination path" in joined_warnings.lower()
            assert "Copy" in joined_warnings

            # 3. Uncheck disabled-by-default checkbox to import active immediately
            chk_disabled.toggle()
            await pilot.pause(0.1)
            assert modal.import_disabled is False

            modal.action_merge_rules()
            await pilot.pause(0.1)

            assert settings.KEYWORD_RULES.get("receipt") == "Documents/Receipts"
            assert "receipt" not in settings.DISABLED_KEYWORD_RULES
            unarch = next(
                p for p in settings.POLICIES if p.get("expression") == "*.zip"
            )
            assert unarch["action"] == "unarchive"
            assert unarch["target_path"] == "Archives/Unpacked"
            assert unarch.get("enabled", True) is True

    asyncio.run(_test())


def test_modal_push_immediate_teardown_does_not_raise_no_matches(tmp_path):
    """Verify pushing DirectorySelectModal or RuleImportModal right before app exit does not raise NoMatches."""
    import asyncio

    from app.ui.modals.edit import DirectorySelectModal
    from app.ui.tui import AutoSorterTUI

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings._settings_model.AI_CONSENT_GRANTED = True
    workspace = tmp_path / "empty_workspace"
    workspace.mkdir(exist_ok=True)

    async def _test_dir_modal():
        app = AutoSorterTUI(
            settings=settings, base_dir=str(workspace), skip_wizard=True
        )
        async with app.run_test():
            app.push_screen(DirectorySelectModal(str(workspace)))
            assert isinstance(app.screen, DirectorySelectModal)

    async def _test_rule_import_modal():
        app = AutoSorterTUI(
            settings=settings, base_dir=str(workspace), skip_wizard=True
        )
        async with app.run_test():
            app.push_screen(RuleImportModal(settings))
            assert isinstance(app.screen, RuleImportModal)

    asyncio.run(_test_dir_modal())
    asyncio.run(_test_rule_import_modal())
