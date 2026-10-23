"""Unit tests for extensible modular rule importer framework and merge engine."""

import json
import plistlib
import tempfile
from pathlib import Path

import pytest

from app.config import AppSettings
from app.core.rule_importers import (
    BaseRuleImporter,
    DropItRuleImporter,
    HazelRuleImporter,
    ImportResult,
    RuleConflictAnalyzer,
    TagSpacesRuleImporter,
    get_rule_importer,
    normalize_rule_path,
)


def test_rule_conflict_analyzer():
    """Verify RuleConflictAnalyzer detects keyword and policy target path collisions."""
    with tempfile.NamedTemporaryFile(suffix=".json") as f:
        settings = AppSettings(filepath=f.name)
        settings.KEYWORD_RULES = {
            "invoice": "Local/Invoices",
            "receipt": "Local/Receipts",
        }
        settings.POLICIES = [
            {
                "type": "pattern",
                "expression": "*.pdf",
                "target_path": "Local/PDFs",
                "priority": 90,
            }
        ]

        # 1. Incoming result with conflicts and non-conflicts
        import_res = ImportResult(
            keyword_rules={
                "invoice": "Hazel/Invoices",  # Differing target -> CONFLICT
                "receipt": "Local/Receipts",  # Same target -> NO CONFLICT
                "tax": "Hazel/Taxes",  # New keyword -> NO CONFLICT
            },
            policies=[
                {
                    "type": "pattern",
                    "expression": "*.pdf",
                    "target_path": "Hazel/PDFs",  # Differing target -> CONFLICT
                    "priority": 75,
                },
                {
                    "type": "pattern",
                    "expression": "*.docx",  # New policy -> NO CONFLICT
                    "target_path": "Hazel/Docs",
                    "priority": 75,
                },
            ],
        )

        conflicts = RuleConflictAnalyzer.analyze(import_res, settings)
        assert len(conflicts) == 2

        kw_conflict = next(c for c in conflicts if c.rule_type == "keyword")
        assert kw_conflict.identifier == "invoice"
        assert kw_conflict.existing_target == "Local/Invoices"
        assert kw_conflict.imported_target == "Hazel/Invoices"

        pol_conflict = next(c for c in conflicts if c.rule_type == "policy")
        assert pol_conflict.identifier == "*.pdf (pattern)"
        assert pol_conflict.existing_target == "Local/PDFs"
        assert pol_conflict.imported_target == "Hazel/PDFs"


def test_rule_conflict_resolutions_overwrite_and_skip():
    """Verify merge_into_settings respects overwrite and granular resolution choices."""
    with tempfile.NamedTemporaryFile(suffix=".json") as f:
        settings = AppSettings(filepath=f.name)
        settings.KEYWORD_RULES = {
            "invoice": "Local/Invoices",
            "statement": "Local/Statements",
        }
        settings.POLICIES = [
            {
                "type": "pattern",
                "expression": "*.pdf",
                "target_path": "Local/PDFs",
                "priority": 90,
            }
        ]

        import_res = ImportResult(
            keyword_rules={
                "invoice": "Hazel/Invoices",
                "statement": "Hazel/Statements",
            },
            policies=[
                {
                    "type": "pattern",
                    "expression": "*.pdf",
                    "target_path": "Hazel/PDFs",
                    "priority": 75,
                }
            ],
        )

        # Apply custom resolutions: invoice -> overwrite, statement -> keep_existing, *.pdf (pattern) -> overwrite
        resolutions = {
            "invoice": "overwrite",
            "statement": "keep_existing",
            "*.pdf (pattern)": "overwrite",
        }

        kw, pol = import_res.merge_into_settings(settings, resolutions=resolutions)

        # invoice overwritten
        assert kw["invoice"] == "Hazel/Invoices"
        # statement kept as local
        assert kw["statement"] == "Local/Statements"
        # *.pdf policy overwritten to Hazel/PDFs
        pdf_policy = next(p for p in pol if p["expression"] == "*.pdf")
        assert pdf_policy["target_path"] == "Hazel/PDFs"


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


def test_non_destructive_rule_merging(tmp_path):
    """Verify merge logic preserves pre-existing custom user rules."""
    settings_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=settings_file)
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


def test_merge_into_settings_propagates_save_exception():
    """Verify ImportResult.merge_into_settings propagates save exceptions from settings._save()."""
    import pytest

    from app.core.rule_importers import ImportResult

    class FailingSettings:
        KEYWORD_RULES = {}
        POLICIES = []

        def _save(self):
            raise OSError("Write permission denied")

    res = ImportResult(keyword_rules={"test": "Folder"})
    settings = FailingSettings()

    with pytest.raises(OSError, match="Write permission denied"):
        res.merge_into_settings(settings)


def test_normalize_and_sanitize_path_helpers():
    """Verify normalize_rule_path and _sanitize_path handle empty/falsy inputs and edge cases."""
    assert normalize_rule_path("") == ""
    assert normalize_rule_path(None) == ""
    assert normalize_rule_path(' "C:\\Path\\To\\Folder\\" ') == "C:/Path/To/Folder/"

    class TestImporter(BaseRuleImporter):
        def parse_file(self, file_path):
            pass

        def can_import(self, file_path):
            pass

    importer = TestImporter()
    assert importer._sanitize_path("") == ""
    assert importer._sanitize_path(None) == ""
    assert importer._sanitize_path('"Folder\\Subfolder"') == "Folder/Subfolder"
    assert importer._is_script_action(12345) is False


def test_rule_conflict_analyzer_and_merge_edge_cases(tmp_path):
    """Verify RuleConflictAnalyzer and merge_into_settings handle empty or invalid rule identifiers gracefully."""
    # Analyze with None parameters
    assert RuleConflictAnalyzer.analyze(None, None) == []

    settings_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=settings_file)
    settings.KEYWORD_RULES = {"invoice": "Target/Invoices"}
    settings.POLICIES = [
        {
            "type": "pattern",
            "expression": "*.pdf",
            "target_path": "Target/PDFs",
            "priority": 75,
        }
    ]

    # Import result with empty keys/expressions
    import_res = ImportResult(
        keyword_rules={
            "": "Target/Empty",
            "   ": "Target/Spaces",
            "invoice": "NewTarget/Invoices",
        },
        policies=[
            {"type": "pattern", "expression": "", "target_path": "Target/EmptyExpr"},
            {
                "type": "pattern",
                "expression": "*.pdf",
                "target_path": "NewTarget/PDFs",
            },
        ],
    )

    conflicts = RuleConflictAnalyzer.analyze(import_res, settings)
    assert len(conflicts) == 2

    kw, pol = import_res.merge_into_settings(settings)
    assert "" not in kw
    assert "   " not in kw


def test_hazel_importer_can_import_os_error(tmp_path):
    """Verify HazelRuleImporter.can_import handles OSError during file read."""
    # Subdirectory path without plist/hazelrules extension triggers OSError on open()
    sub_dir = tmp_path / "test_dir.dat"
    sub_dir.mkdir()

    importer = HazelRuleImporter()
    assert importer.can_import(sub_dir) is False


def test_hazel_importer_file_errors_and_structures(tmp_path):
    """Verify HazelRuleImporter handles missing files, empty plists, top-level lists, and invalid rule structures."""
    importer = HazelRuleImporter()

    # 1. Missing file
    res1 = importer.parse_file(tmp_path / "nonexistent.hazelrules")
    assert any("File not found" in w for w in res1.warnings)

    # 2. Empty plist
    empty_file = tmp_path / "empty.hazelrules"
    with open(empty_file, "wb") as f:
        plistlib.dump({}, f)
    res2 = importer.parse_file(empty_file)
    assert any("is empty" in w for w in res2.warnings)

    # 3. Top-level plist is a list of rule dicts
    list_file = tmp_path / "rules_list.hazelrules"
    with open(list_file, "wb") as f:
        plistlib.dump(
            [
                {
                    "NSRuleName": "ListRule1",
                    "DestinationPath": "Docs/List",
                    "NSRuleConditions": [],
                }
            ],
            f,
        )
    res3 = importer.parse_file(list_file)
    assert res3.keyword_rules.get("listrule1") == "Docs/List"

    # 4. Unrecognized Hazel structure (Rules is string)
    unrec_file = tmp_path / "unrec.hazelrules"
    with open(unrec_file, "wb") as f:
        plistlib.dump({"Rules": "not_a_list"}, f)
    res4 = importer.parse_file(unrec_file)
    assert any("Unrecognized Hazel rule package structure" in w for w in res4.warnings)

    # 5. Rules list with non-dict items
    nondict_file = tmp_path / "nondict.hazelrules"
    with open(nondict_file, "wb") as f:
        plistlib.dump({"Rules": ["not_a_dict", 123, "string"]}, f)
    res5 = importer.parse_file(nondict_file)
    assert len(res5.keyword_rules) == 0


def test_hazel_importer_actions_and_targets(tmp_path):
    """Verify HazelRuleImporter handles string actions, top-level fallback target paths, and missing destinations."""
    importer = HazelRuleImporter()

    # Rule with string action containing path, rule with top-level destination fallback, and rule without target
    data = {
        "Rules": [
            {
                "NSRuleName": "StringActionRule",
                "NSRuleActions": ["Financial/Invoices"],
                "NSRuleConditions": [],
            },
            {
                "NSRuleName": "TopLevelFallbackRule",
                "DestinationPath": "Fallback/Folder",
                "NSRuleConditions": [],
            },
            {
                "NSRuleName": "NoDestinationRule",
                "NSRuleConditions": [],
            },
        ]
    }
    plist_file = tmp_path / "actions_test.hazelrules"
    with open(plist_file, "wb") as f:
        plistlib.dump(data, f)

    res = importer.parse_file(plist_file)
    assert res.keyword_rules.get("stringactionrule") == "Financial/Invoices"
    assert res.keyword_rules.get("toplevelfallbackrule") == "Fallback/Folder"
    assert any("has no target destination folder" in w for w in res.warnings)
    assert any(u.get("rule") == "NoDestinationRule" for u in res.unsupported)


def test_hazel_importer_condition_branches(tmp_path):
    """Verify HazelRuleImporter covers condition parsing branches (single word vs phrase, empty/None values, patterns)."""
    importer = HazelRuleImporter()

    data = {
        "Rules": [
            {
                "NSRuleName": "ConditionTestRule",
                "DestinationPath": "Target/Folder",
                "NSRuleConditions": [
                    "not_a_dict",  # non-dict condition
                    {"target": "Name"},  # value is None
                    {"target": "Name", "value": "   "},  # empty string value
                    {
                        "target": "Name",
                        "value": "receipt",
                    },  # single-word alphanumeric keyword
                    {"target": "Kind", "value": "bank statement"},  # phrase policy
                    {"attribute": "pattern", "value": "tax_*"},  # pattern attribute
                ],
            },
            {
                "NSRuleName": "Phrase Rule Without Conditions",
                "DestinationPath": "Target/Phrases",
                "NSRuleConditions": [],
            },
        ]
    }
    plist_file = tmp_path / "cond_test.hazelrules"
    with open(plist_file, "wb") as f:
        plistlib.dump(data, f)

    res = importer.parse_file(plist_file)

    # Keyword rule generated for single-word condition
    assert res.keyword_rules.get("receipt") == "Target/Folder"

    # Policies generated for phrase condition and pattern attribute
    exprs = [p["expression"] for p in res.policies]
    assert "bank statement" in exprs
    assert "*.tax_*" in exprs
    assert "Phrase Rule Without Conditions" in exprs


def test_dropit_importer_file_errors_and_empty_content(tmp_path):
    """Verify DropItRuleImporter handles OSError on read, missing files, empty files, and dispatching."""
    importer = DropItRuleImporter()

    # 1. Missing file
    res1 = importer.parse_file(tmp_path / "nonexistent.xml")
    assert any("File not found" in w for w in res1.warnings)

    # 2. OSError when file is a directory
    sub_dir = tmp_path / "dropit_dir"
    sub_dir.mkdir()
    res2 = importer.parse_file(sub_dir)
    assert any("Failed to read file" in w for w in res2.warnings)

    # 3. Empty file
    empty_file = tmp_path / "empty.dropit"
    empty_file.write_text("   \n  ", encoding="utf-8")
    res3 = importer.parse_file(empty_file)
    assert any("is empty" in w for w in res3.warnings)


def test_dropit_xml_importer_edge_cases(tmp_path):
    """Verify DropIt XML importer handles root elements as items, default names, script actions, and missing destinations."""
    importer = DropItRuleImporter()

    # XML with single Association root, script execution action, and missing destination
    xml_content = """<?xml version="1.0" encoding="UTF-8"?>
    <Association>
        <Pattern>*.doc</Pattern>
        <Destination>Work/Docs</Destination>
        <Action>Open with python.exe</Action>
    </Association>
    """
    xml_file = tmp_path / "single_item.xml"
    xml_file.write_text(xml_content, encoding="utf-8")

    res1 = importer.parse_file(xml_file)
    assert res1.source_format == "DropIt XML Profile"
    assert any("command/script execution" in w for w in res1.warnings)

    # XML item without destination
    xml_content_no_dest = """<?xml version="1.0" encoding="UTF-8"?>
    <Profile>
        <Item>
            <Name>No Dest Item</Name>
            <Pattern>*.txt</Pattern>
        </Item>
    </Profile>
    """
    xml_no_dest_file = tmp_path / "no_dest.xml"
    xml_no_dest_file.write_text(xml_content_no_dest, encoding="utf-8")

    res2 = importer.parse_file(xml_no_dest_file)
    assert any("has no target directory" in w for w in res2.warnings)


def test_dropit_json_importer_edge_cases(tmp_path):
    """Verify DropIt JSON importer handles data dicts, non-list structures, non-dict elements, script actions, and missing destinations."""
    importer = DropItRuleImporter()

    # 1. JSON dict with 'profile' key and script action
    json_data = {
        "profile": [
            {
                "Name": "Script Rule",
                "Pattern": "*.py",
                "Destination": "Code/Python",
                "Action": "exec script",
            },
            {
                "Name": "No Dest Rule",
                "Pattern": "*.log",
            },
        ]
    }
    json_file = tmp_path / "profile.json"
    json_file.write_text(json.dumps(json_data), encoding="utf-8")

    res1 = importer.parse_file(json_file)
    assert res1.source_format == "DropIt JSON Profile"
    assert any("command/script execution" in w for w in res1.warnings)
    assert any("has no target directory" in w for w in res1.warnings)

    # 2. JSON dict single rule representation
    json_single = {
        "Name": "Single JSON Rule",
        "Pattern": "invoice",
        "Destination": "Finance/Invoices",
    }
    single_file = tmp_path / "single.json"
    single_file.write_text(json.dumps(json_single), encoding="utf-8")

    res2 = importer.parse_file(single_file)
    assert res2.keyword_rules.get("invoice") == "Finance/Invoices"

    # 3. Unrecognized JSON profile structure (rules key is int)
    unrec_data = {"rules": 999}
    unrec_file = tmp_path / "unrec.json"
    unrec_file.write_text(json.dumps(unrec_data), encoding="utf-8")

    res3 = importer.parse_file(unrec_file)
    assert any("Unrecognized DropIt JSON profile structure" in w for w in res3.warnings)

    # 4. JSON rules list with non-dict elements
    nondict_data = [123, "string", None]
    nondict_file = tmp_path / "nondict.json"
    nondict_file.write_text(json.dumps(nondict_data), encoding="utf-8")

    res4 = importer.parse_file(nondict_file)
    assert len(res4.keyword_rules) == 0


def test_dropit_pattern_splitting_and_fallbacks(tmp_path):
    """Verify DropIt pattern splitting with ;, |, and , and empty pattern fallback."""
    importer = DropItRuleImporter()

    json_data = [
        {
            "Name": "MultiPatternRule",
            "Pattern": "report;*.xlsx|data_export,summary report",
            "Destination": "Reports/Exports",
        },
        {
            "Name": "FallbackToName",
            "Pattern": "",
            "Destination": "Reports/Fallback",
        },
    ]
    json_file = tmp_path / "patterns.json"
    json_file.write_text(json.dumps(json_data), encoding="utf-8")

    res = importer.parse_file(json_file)

    # 'report' -> keyword rule
    assert res.keyword_rules.get("report") == "Reports/Exports"

    # '*.xlsx' -> pattern policy
    xlsx_policies = [p for p in res.policies if p["expression"] == "*.xlsx"]
    assert len(xlsx_policies) == 1

    # 'data_export' -> keyword rule
    assert res.keyword_rules.get("data_export") == "Reports/Exports"

    # 'summary report' -> keyword policy
    sum_policies = [p for p in res.policies if p["expression"] == "summary report"]
    assert len(sum_policies) == 1

    # Empty pattern fallback to rule name ('fallbacktoname') -> keyword rule
    assert res.keyword_rules.get("fallbacktoname") == "Reports/Fallback"


def test_tagspaces_importer_can_import_and_detection_branches(tmp_path):
    """Verify TagSpacesRuleImporter.can_import and _is_tagspaces_data branches."""
    importer = TagSpacesRuleImporter()

    # 1. Non-existent JSON file -> False
    assert importer.can_import(tmp_path / "nonexistent.json") is False

    # 2. Corrupted JSON file -> False
    corrupt_file = tmp_path / "corrupt.json"
    corrupt_file.write_text("{bad json", encoding="utf-8")
    assert importer.can_import(corrupt_file) is False

    # 3. TagSpaces dict key branches (tagGroup, smart_rules, etc.)
    ts_dict = tmp_path / "ts_dict.json"
    ts_dict.write_text(json.dumps({"tagGroup": {}}), encoding="utf-8")
    assert importer.can_import(ts_dict) is True

    # 4. DropIt key exclusion -> False
    dropit_in_json = tmp_path / "dropit_exclusion.json"
    dropit_in_json.write_text(json.dumps({"Pattern": "*.pdf"}), encoding="utf-8")
    assert importer.can_import(dropit_in_json) is False

    # 5. List of dicts with smart rule or tag spaces keys
    ts_list = tmp_path / "ts_list.json"
    ts_list.write_text(json.dumps([{"name": "Rule", "color": "red"}]), encoding="utf-8")
    assert importer.can_import(ts_list) is True

    # 6. List of dicts with matchers/conditions
    ts_matchers = tmp_path / "ts_matchers.json"
    ts_matchers.write_text(
        json.dumps([{"title": "Rule", "matchers": []}]), encoding="utf-8"
    )
    assert importer.can_import(ts_matchers) is True

    # 7. List of DropIt items -> False
    dropit_list = tmp_path / "dropit_list.json"
    dropit_list.write_text(
        json.dumps([{"Pattern": "*.pdf", "Destination": "dest"}]), encoding="utf-8"
    )
    assert importer.can_import(dropit_list) is False


def test_tagspaces_importer_file_errors_empty_and_no_convertible_rules(tmp_path):
    """Verify TagSpacesRuleImporter handles missing files, empty files, and empty convertible data."""
    importer = TagSpacesRuleImporter()

    # 1. Non-existent file
    res1 = importer.parse_file(tmp_path / "nonexistent.json")
    assert any("File not found" in w for w in res1.warnings)

    # 2. Empty data ({})
    empty_file = tmp_path / "empty_ts.json"
    empty_file.write_text("{}", encoding="utf-8")
    res2 = importer.parse_file(empty_file)
    assert any("is empty" in w for w in res2.warnings)

    # 3. Valid JSON with no convertible tag groups or smart rules
    no_rules_file = tmp_path / "no_rules.json"
    no_rules_file.write_text(json.dumps({"tagGroups": []}), encoding="utf-8")
    res3 = importer.parse_file(no_rules_file)
    assert any(
        "No convertible TagSpaces tag groups or smart rules found" in w
        for w in res3.warnings
    )


def test_tagspaces_taxonomy_traversal_nested_structures(tmp_path):
    """Verify TagSpaces taxonomy traversal with dict/string raw_tags, raw_children, root title skipping, and missing targets."""
    importer = TagSpacesRuleImporter()

    ts_data = {
        "groups": [
            {
                "title": "root",  # root title skipped in path construction
                "tags": [
                    "root_tag",  # Tag at root level has empty target_dir -> warning
                    {"title": "dict_tag"},
                    {
                        "subgroups": [{"title": "SubGroup", "tags": ["sub_tag"]}]
                    },  # dict tag with subgroups
                ],
                "children": [
                    "string_child_tag",  # string child item
                    {
                        "title": "Projects",
                        "children": [{"name": "wildcard_tag*"}],
                    },
                ],
            }
        ]
    }

    ts_file = tmp_path / "taxonomy.json"
    ts_file.write_text(json.dumps(ts_data), encoding="utf-8")

    res = importer.parse_file(ts_file)

    # Root level tag without target folder generates warning
    assert any(
        "Tag 'root_tag' in group has no target directory" in w for w in res.warnings
    )

    # Nested sub_tag -> target_dir "SubGroup"
    assert res.keyword_rules.get("sub_tag") == "SubGroup"

    # Wildcard tag -> pattern policy under "Projects"
    wildcard_policies = [p for p in res.policies if p["expression"] == "wildcard_tag*"]
    assert len(wildcard_policies) == 1
    assert wildcard_policies[0]["target_path"] == "Projects"


def test_tagspaces_smart_rules_actions_and_conditions(tmp_path):
    """Verify TagSpaces smart rules parsing for script actions, action dict targets, and string/extension condition matchers."""
    importer = TagSpacesRuleImporter()

    ts_smart_data = [
        {
            "name": "ActionTargetRule",
            "actions": [{"target": "Smart/ActionTarget"}],
            "conditions": "string_condition",
        },
        {
            "name": "TopLevelScriptRule",
            "actions": [{"type": "script", "command": "python execute.py"}],
            "destination": "Smart/ScriptTarget",
            "conditions": [
                {"type": "ext", "val": "pdf"},
                {"type": "extension", "val": ".docx"},
                {"type": "name", "val": "   "},  # empty condition value skipped
            ],
        },
        {
            "name": "NoTargetSmartRule",
            "conditions": ["test"],
        },
    ]

    ts_file = tmp_path / "smart_rules_test.json"
    ts_file.write_text(json.dumps(ts_smart_data), encoding="utf-8")

    res = importer.parse_file(ts_file)

    # ActionTargetRule keyword rule
    assert res.keyword_rules.get("string_condition") == "Smart/ActionTarget"

    # TopLevelScriptRule script detection
    assert any("command/script execution" in w for w in res.warnings)

    # Extension matchers mapped to pattern policies (*.pdf, *.docx)
    exprs = [p["expression"] for p in res.policies]
    assert "*.pdf" in exprs
    assert "*.docx" in exprs

    # NoTargetSmartRule missing target directory
    assert any("has no target directory" in w for w in res.warnings)


def test_get_rule_importer_fallback_extensions_and_unsupported():
    """Verify get_rule_importer fallback extension logic when can_import returns False."""
    from unittest.mock import patch

    # Mock can_import to return False so fallback extension checks are exercised
    with (
        patch.object(TagSpacesRuleImporter, "can_import", return_value=False),
        patch.object(HazelRuleImporter, "can_import", return_value=False),
        patch.object(DropItRuleImporter, "can_import", return_value=False),
    ):
        imp1 = get_rule_importer("nonexistent_taggroup.json")
        assert isinstance(imp1, TagSpacesRuleImporter)

        imp2 = get_rule_importer("nonexistent.hazelrules")
        assert isinstance(imp2, HazelRuleImporter)

        imp3 = get_rule_importer("nonexistent.dropit")
        assert isinstance(imp3, DropItRuleImporter)

    # Test unsupported extension raises ValueError
    with pytest.raises(ValueError, match="Unsupported rule file format"):
        get_rule_importer("unsupported_format.xyz123")


def test_rule_importers_coverage_gaps(tmp_path):
    """Verify abstract methods, single item XML, tagspaces data detection edge cases, and string/dict action forms."""

    # 1. BaseRuleImporter abstract methods
    class DummyImporter(BaseRuleImporter):
        def parse_file(self, file_path):
            return super().parse_file(file_path)

        def can_import(self, file_path):
            return super().can_import(file_path)

    dummy = DummyImporter()
    dummy.parse_file("f")
    dummy.can_import("f")

    # 2. DropIt XML single element without child tags matching association/rule/item
    xml_single = tmp_path / "single_root.xml"
    xml_single.write_text(
        '<?xml version="1.0"?><Association><Name>RootAssoc</Name><Destination>Target/Folder</Destination><Pattern>*.pdf</Pattern></Association>',
        encoding="utf-8",
    )
    res_xml = DropItRuleImporter().parse_file(xml_single)
    assert len(res_xml.policies) == 1

    # 3. TagSpaces data detection and group extraction edge cases
    ts_importer = TagSpacesRuleImporter()
    assert ts_importer._is_tagspaces_data([]) is False
    assert (
        ts_importer._is_tagspaces_data({"title": "Taxonomy", "subgroups": []}) is True
    )
    assert ts_importer._is_tagspaces_data([{"title": "G1", "isSmart": True}]) is True
    assert ts_importer._is_tagspaces_data("invalid_type") is False

    # 4. Single Hazel dict rule
    single_hazel = tmp_path / "single_hazel.hazelrules"
    with open(single_hazel, "wb") as f:
        plistlib.dump({"NSRuleName": "SingleRule", "DestinationPath": "Single/Dest"}, f)
    res_hazel = HazelRuleImporter().parse_file(single_hazel)
    assert res_hazel.keyword_rules.get("singlerule") == "Single/Dest"

    # 5. TagSpaces raw_tags/raw_children as string or dict, empty tags, actions string/dict, and smart rule wildcard matchers
    ts_data = {
        "title": "StandaloneGroup",
        "tags": "single_string_tag",
        "children": {
            "title": "SubFolder",
            "tags": [{"title": "tag_in_dict"}, {"title": "   "}],  # empty tag_clean
        },
        "smartRules": [
            {
                "title": "SmartRuleStringAction",
                "destination": "Smart/TargetFolder",
                "actions": "move",  # string action
                "conditions": [
                    "*.png",
                    "   ",
                ],  # wildcard condition and empty condition
            }
        ],
    }
    ts_file = tmp_path / "ts_gaps.json"
    ts_file.write_text(json.dumps(ts_data), encoding="utf-8")

    res_ts = ts_importer.parse_file(ts_file)
    assert res_ts.keyword_rules.get("single_string_tag") == "StandaloneGroup"
    assert res_ts.keyword_rules.get("tag_in_dict") == "StandaloneGroup/SubFolder"
    png_policies = [p for p in res_ts.policies if p["expression"] == "*.png"]
    assert len(png_policies) == 1
