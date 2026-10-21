"""Unit tests for RuleImportModal interactive preview and rule merging dialog."""

import json
import plistlib
import tempfile
from pathlib import Path

from app.config import AppSettings
from app.core.rule_importers import get_rule_importer
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


def test_rule_import_modal_tagspaces_parsing_and_merging():
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

    with tempfile.NamedTemporaryFile(suffix=".json") as sf:
        settings = AppSettings(filepath=sf.name)
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
