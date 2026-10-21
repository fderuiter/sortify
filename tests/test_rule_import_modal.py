"""Unit tests for RuleImportModal interactive preview and rule merging dialog."""

import plistlib
import tempfile
from pathlib import Path

from app.config import AppSettings
from app.ui.tui import RuleImportModal


def test_rule_import_modal_instantiation():
    """Verify RuleImportModal instantiates and mounts correctly."""
    with tempfile.NamedTemporaryFile(suffix=".json") as f:
        settings = AppSettings(filepath=f.name)
        modal = RuleImportModal(settings)
        assert modal.settings is settings
        assert modal.current_result is None


def test_rule_import_modal_parsing_and_merging():
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

    with tempfile.NamedTemporaryFile(suffix=".json") as sf:
        settings = AppSettings(filepath=sf.name)
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


def test_rule_import_modal_conflict_preview_and_resolution():
    """Verify RuleImportModal displays conflicts and supports interactive resolution selection."""

    from app.core.rule_importers import ImportResult, RuleConflictAnalyzer

    with tempfile.NamedTemporaryFile(suffix=".json") as sf:
        settings = AppSettings(filepath=sf.name)
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
        modal.conflict_resolutions = {
            c.identifier: c.resolution for c in modal.conflicts
        }

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
