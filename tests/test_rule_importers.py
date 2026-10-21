"""Unit tests for extensible modular rule importer framework and merge engine."""

import json
import plistlib
import tempfile
from pathlib import Path

import pytest

from app.config import AppSettings
from app.core.rule_importers import (
    DropItRuleImporter,
    HazelRuleImporter,
    ImportResult,
    TagSpacesRuleImporter,
    get_rule_importer,
)


def test_hazel_rule_importer_plist():
    """Verify HazelRuleImporter parses .hazelrules plist files into KEYWORD_RULES and POLICIES."""
    hazel_data = {
        "Rules": [
            {
                "NSRuleName": "Invoices & Receipts",
                "NSRuleConditions": [
                    {
                        "target": "Name",
                        "operator": "contains",
                        "value": "invoice",
                    },
                    {
                        "target": "Extension",
                        "operator": "is",
                        "value": "pdf",
                    },
                ],
                "NSRuleActions": [
                    {
                        "DestinationPath": "Financial/Invoices",
                    }
                ],
            },
            {
                "NSRuleName": "Run Malicious Script",
                "NSRuleConditions": [
                    {
                        "target": "Name",
                        "operator": "contains",
                        "value": "tax",
                    }
                ],
                "NSRuleActions": [
                    {
                        "NSRuleActionType": "NSRuleActionRunScript",
                        "script": "import os; os.system('rm -rf /')",
                    },
                    {
                        "DestinationPath": "Taxes/2025",
                    },
                ],
            },
        ]
    }

    with tempfile.NamedTemporaryFile(
        suffix=".hazelrules", mode="wb", delete=False
    ) as f:
        plistlib.dump(hazel_data, f)
        temp_path = f.name

    try:
        importer = HazelRuleImporter()
        assert importer.can_import(temp_path) is True

        res = importer.parse_file(temp_path)
        assert isinstance(res, ImportResult)
        assert "invoice" in res.keyword_rules
        assert res.keyword_rules["invoice"] == "Financial/Invoices"

        # Verify pattern policy generated for extension rule
        pdf_policies = [p for p in res.policies if p["expression"] in ("*.pdf", "pdf")]
        assert len(pdf_policies) >= 1

        # Verify script action was flagged as unsupported and warning added
        assert len(res.unsupported) >= 1
        assert any("script" in w.lower() for w in res.warnings)
    finally:
        Path(temp_path).unlink(missing_ok=True)


def test_dropit_rule_importer_xml():
    """Verify DropItRuleImporter parses DropIt XML profile export files."""
    xml_content = """<?xml version="1.0" encoding="UTF-8"?>
    <Profile>
        <Association>
            <Name>Documents</Name>
            <Pattern>*.pdf;*.docx;Invoice_*</Pattern>
            <Destination>Work/Documents</Destination>
            <Action>Move</Action>
        </Association>
        <Association>
            <Name>Images</Name>
            <Pattern>*.jpg,*.png</Pattern>
            <Destination>Media/Photos</Destination>
            <Action>Move</Action>
        </Association>
    </Profile>
    """

    with tempfile.NamedTemporaryFile(
        suffix=".xml", mode="w", encoding="utf-8", delete=False
    ) as f:
        f.write(xml_content)
        temp_path = f.name

    try:
        importer = DropItRuleImporter()
        assert importer.can_import(temp_path) is True

        res = importer.parse_file(temp_path)
        assert isinstance(res, ImportResult)

        policy_exprs = [p["expression"] for p in res.policies]
        assert "*.pdf" in policy_exprs
        assert "*.docx" in policy_exprs
        assert "Invoice_*" in policy_exprs
        assert "*.jpg" in policy_exprs
        assert "*.png" in policy_exprs
    finally:
        Path(temp_path).unlink(missing_ok=True)


def test_dropit_rule_importer_json():
    """Verify DropItRuleImporter parses DropIt JSON profile export files."""
    json_data = [
        {
            "Name": "Medical Records",
            "Pattern": "prescription;lab_result",
            "Destination": "Medical/Records",
            "Action": "Move",
        },
        {
            "Name": "Executable Script Action",
            "Pattern": "*.sh",
            "Destination": "Scripts",
            "Action": "Open with bash",
        },
    ]

    with tempfile.NamedTemporaryFile(
        suffix=".json", mode="w", encoding="utf-8", delete=False
    ) as f:
        json.dump(json_data, f)
        temp_path = f.name

    try:
        importer = DropItRuleImporter()
        assert importer.can_import(temp_path) is True

        res = importer.parse_file(temp_path)
        assert isinstance(res, ImportResult)
        assert res.keyword_rules.get("prescription") == "Medical/Records"
        assert res.keyword_rules.get("lab_result") == "Medical/Records"

        # Verify script action was flagged
        assert len(res.unsupported) >= 1
    finally:
        Path(temp_path).unlink(missing_ok=True)


def test_get_rule_importer_factory():
    """Verify factory function auto-detects importer strategies."""
    with (
        tempfile.NamedTemporaryFile(suffix=".hazelrules") as f1,
        tempfile.NamedTemporaryFile(suffix=".xml") as f2,
        tempfile.NamedTemporaryFile(suffix=".json") as f3,
        tempfile.NamedTemporaryFile(suffix="taggroup.json") as f4,
    ):
        imp1 = get_rule_importer(f1.name)
        assert isinstance(imp1, HazelRuleImporter)

        imp2 = get_rule_importer(f2.name)
        assert isinstance(imp2, DropItRuleImporter)

        imp3 = get_rule_importer(f3.name)
        assert isinstance(imp3, DropItRuleImporter)

        imp4 = get_rule_importer(f4.name)
        assert isinstance(imp4, TagSpacesRuleImporter)

    with pytest.raises(ValueError):
        get_rule_importer("/tmp/invalid_file.xyz_unknown")


def test_non_destructive_rule_merging():
    """Verify merge logic preserves pre-existing custom user rules."""
    with tempfile.NamedTemporaryFile(suffix=".json") as f:
        settings = AppSettings(filepath=f.name)
        settings.KEYWORD_RULES = {"invoice": "MyCustom/Invoices"}
        settings.POLICIES = [
            {
                "type": "pattern",
                "expression": "*.pdf",
                "target_path": "MyCustom/PDFs",
                "priority": 90,
                "halting": False,
            }
        ]

        import_res = ImportResult(
            keyword_rules={
                "invoice": "Hazel/Invoices",  # Duplicate key -> should NOT overwrite
                "statement": "Financial/Statements",  # New key -> should be added
            },
            policies=[
                {
                    "type": "pattern",
                    "expression": "*.pdf",  # Duplicate expression -> should NOT be duplicated
                    "target_path": "Hazel/PDFs",
                    "priority": 75,
                    "halting": False,
                },
                {
                    "type": "pattern",
                    "expression": "*.docx",  # New policy -> should be added
                    "target_path": "Hazel/Docs",
                    "priority": 75,
                    "halting": False,
                },
            ],
        )

        kw, pol = import_res.merge_into_settings(settings)

        # Pre-existing custom rule MUST be preserved
        assert kw["invoice"] == "MyCustom/Invoices"
        assert kw["statement"] == "Financial/Statements"

        # Duplicate policy expression MUST NOT be duplicated
        pdf_policies = [p for p in pol if p["expression"] == "*.pdf"]
        assert len(pdf_policies) == 1
        assert pdf_policies[0]["target_path"] == "MyCustom/PDFs"

        # New policy MUST be added
        docx_policies = [p for p in pol if p["expression"] == "*.docx"]
        assert len(docx_policies) == 1


def test_resilience_malformed_files():
    """Verify importers do not crash when encountering malformed files."""
    with tempfile.NamedTemporaryFile(suffix=".hazelrules", mode="w", delete=False) as f:
        f.write("corrupted plist content >>> <<< !!!")
        temp_path1 = f.name

    with tempfile.NamedTemporaryFile(suffix=".xml", mode="w", delete=False) as f:
        f.write("<unclosed_tag>invalid xml content")
        temp_path2 = f.name

    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        f.write("{invalid json content, missing quotes}")
        temp_path3 = f.name

    try:
        res1 = HazelRuleImporter().parse_file(temp_path1)
        assert len(res1.warnings) >= 1

        res2 = DropItRuleImporter().parse_file(temp_path2)
        assert len(res2.warnings) >= 1

        res3 = DropItRuleImporter().parse_file(temp_path3)
        assert len(res3.warnings) >= 1

        res4 = TagSpacesRuleImporter().parse_file(temp_path3)
        assert len(res4.warnings) >= 1
    finally:
        Path(temp_path1).unlink(missing_ok=True)
        Path(temp_path2).unlink(missing_ok=True)
        Path(temp_path3).unlink(missing_ok=True)


def test_tagspaces_rule_importer_taggroup():
    """Verify TagSpacesRuleImporter parses taggroup.json taxonomy hierarchy."""
    ts_data = {
        "tagGroups": [
            {
                "title": "Financial",
                "tags": ["finance"],
                "children": [
                    {
                        "title": "Invoices",
                        "tags": [
                            {"title": "invoice"},
                            "paid_invoice",
                            {"title": "tax receipt"},
                            {"title": "*.pdf"},
                        ],
                    }
                ],
            }
        ]
    }

    with tempfile.NamedTemporaryFile(
        suffix="taggroup.json", mode="w", encoding="utf-8", delete=False
    ) as f:
        json.dump(ts_data, f)
        temp_path = f.name

    try:
        importer = TagSpacesRuleImporter()
        assert importer.can_import(temp_path) is True

        res = importer.parse_file(temp_path)
        assert isinstance(res, ImportResult)
        assert res.source_format == "TagSpaces (taggroup.json)"

        # Check single-word keywords
        assert res.keyword_rules.get("finance") == "Financial"
        assert res.keyword_rules.get("invoice") == "Financial/Invoices"
        assert res.keyword_rules.get("paid_invoice") == "Financial/Invoices"

        # Check multi-word phrase policy
        multi_word_policies = [
            p for p in res.policies if p["expression"] == "tax receipt"
        ]
        assert len(multi_word_policies) == 1
        assert multi_word_policies[0]["target_path"] == "Financial/Invoices"
        assert multi_word_policies[0]["type"] == "keyword"

        # Check pattern policy for wildcard
        pattern_policies = [p for p in res.policies if p["expression"] == "*.pdf"]
        assert len(pattern_policies) == 1
        assert pattern_policies[0]["target_path"] == "Financial/Invoices"
        assert pattern_policies[0]["type"] == "pattern"
    finally:
        Path(temp_path).unlink(missing_ok=True)


def test_tagspaces_rule_importer_smart_rules():
    """Verify TagSpacesRuleImporter parses smart rules and filters out script actions."""
    ts_smart_data = {
        "smartRules": [
            {
                "title": "PDF Document Router",
                "conditions": [
                    {"field": "extension", "value": "pdf"},
                    {"field": "filename", "value": "monthly statement"},
                ],
                "destination": "Documents/PDFs",
                "actions": [
                    {"type": "move", "target": "Documents/PDFs"},
                    {"type": "script", "command": "python trigger.py"},
                ],
            }
        ]
    }

    with tempfile.NamedTemporaryFile(
        suffix=".json", mode="w", encoding="utf-8", delete=False
    ) as f:
        json.dump(ts_smart_data, f)
        temp_path = f.name

    try:
        importer = TagSpacesRuleImporter()
        assert importer.can_import(temp_path) is True

        res = importer.parse_file(temp_path)
        assert isinstance(res, ImportResult)

        # Check policy rule created for extension matcher (*.pdf)
        pdf_policies = [p for p in res.policies if p["expression"] == "*.pdf"]
        assert len(pdf_policies) == 1
        assert pdf_policies[0]["target_path"] == "Documents/PDFs"
        assert pdf_policies[0]["priority"] == 85

        # Check policy rule created for multi-word phrase condition ("monthly statement")
        phrase_policies = [
            p for p in res.policies if p["expression"] == "monthly statement"
        ]
        assert len(phrase_policies) == 1
        assert phrase_policies[0]["target_path"] == "Documents/PDFs"

        # Verify script action was flagged as unsupported and warning added
        assert len(res.unsupported) >= 1
        assert any("script" in w.lower() for w in res.warnings)
    finally:
        Path(temp_path).unlink(missing_ok=True)
