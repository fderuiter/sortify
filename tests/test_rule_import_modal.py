"""Unit tests for RuleImportModal interactive preview and rule merging dialog."""

import json
import plistlib
import tempfile
from pathlib import Path

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

        # Perform merge action
        modal.action_merge_rules()

        assert settings.KEYWORD_RULES.get("invoice") == "Financial/Invoices"
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

        assert settings.KEYWORD_RULES.get("prescription") == "Medical"
        lab_policies = [
            p for p in settings.POLICIES if p.get("expression") == "lab report"
        ]
        assert len(lab_policies) == 1
        assert lab_policies[0].get("target_path") == "Medical"
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
