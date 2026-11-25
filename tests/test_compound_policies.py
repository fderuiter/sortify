"""Comprehensive tests for compound boolean PolicyRule condition groups, importers, exporters, CLI, and TUI."""

from __future__ import annotations

import json
import plistlib
import subprocess
import sys

import pytest
from textual.widgets import DataTable, Input, Label, Select

from app.config import AppSettings, Settings
from app.core.analyzer import IncrementalAnalyzer
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.metadata import MetadataPass
from app.core.policy_engine import (
    MAX_CONDITION_CLAUSES,
    MAX_POLICY_REGEX_LENGTH,
    PolicyEngine,
    is_masked_by,
    match_pattern_expression,
    validate_policy_conditions,
    validate_safe_regex,
)
from app.core.rule_importers import (
    DropItRuleImporter,
    HazelRuleImporter,
    ImportResult,
    RuleConflictAnalyzer,
    SortifyRuleImporter,
    TagSpacesRuleImporter,
)
from app.core.rule_templates import export_active_rules
from app.ui.modals.drop_zone import DropZoneModal
from app.ui.modals.rules import ActiveRuleManagerModal, RuleEditModal, _PolicyClauseRow
from app.ui.tui import AutoSorterTUI

# ---------------------------------------------------------------------------
# 1. Schema, Guardrails, and Regex / Tree Bounds
# ---------------------------------------------------------------------------


def test_settings_validate_policies_compound_and_guardrails():
    """Verify Settings.validate_policies accepts valid 2-level trees and enforces regex/depth/clause bounds."""
    valid_policies = [
        {
            "type": "pattern",
            "expression": "",
            "target_path": "Finance/Invoices",
            "priority": 90,
            "match_mode": "all",
            "conditions": [
                {"field": "extension", "operator": "equals", "value": "pdf"},
                {
                    "match_mode": "any",
                    "conditions": [
                        {
                            "field": "content",
                            "operator": "contains",
                            "value": "invoice",
                        },
                        {"field": "name", "operator": "regex", "value": r"^inv_\d{4}"},
                    ],
                },
                {
                    "field": "name",
                    "operator": "contains",
                    "value": "draft",
                    "negate": True,
                },
            ],
        }
    ]
    validated = Settings.validate_policies(valid_policies)
    assert len(validated) == 1
    assert validated[0]["match_mode"] == "all"
    # Auto-populated readable summary in required expression field
    assert "ALL(" in validated[0]["expression"]

    # Reject regex exceeding 256 characters
    long_regex = "a" * (MAX_POLICY_REGEX_LENGTH + 1)
    with pytest.raises(ValueError, match="maximum length"):
        Settings.validate_policies(
            [
                {
                    "type": "pattern",
                    "expression": long_regex,
                    "target_path": "Out",
                    "priority": 50,
                    "regex": True,
                }
            ]
        )

    # Reject ReDoS nested quantifiers both at top-level and inside condition clauses
    for redos_pattern in (r"(a+)+$", r"([a-z]+)*", r"(a|aa)+", r"((ab)*)+"):
        with pytest.raises(ValueError, match="nested quantifiers"):
            validate_safe_regex(redos_pattern)
        with pytest.raises(ValueError, match="nested quantifiers"):
            Settings.validate_policies(
                [
                    {
                        "type": "pattern",
                        "expression": "redos",
                        "target_path": "Out",
                        "priority": 50,
                        "match_mode": "all",
                        "conditions": [
                            {
                                "field": "name",
                                "operator": "regex",
                                "value": redos_pattern,
                            }
                        ],
                    }
                ]
            )

    # Reject invalid regex syntax
    with pytest.raises(ValueError, match="Invalid policy regular expression"):
        validate_safe_regex(r"[unclosed_group")

    # Reject condition tree deeper than 2 nesting levels
    three_level_tree = [
        {
            "match_mode": "all",
            "conditions": [
                {
                    "match_mode": "any",
                    "conditions": [
                        {"field": "name", "operator": "contains", "value": "too_deep"}
                    ],
                }
            ],
        }
    ]
    with pytest.raises(ValueError, match="maximum nesting depth"):
        validate_policy_conditions(three_level_tree)

    # Reject condition tree with more than 32 clauses
    too_many_clauses = [
        {"field": "name", "operator": "contains", "value": f"tok_{i}"}
        for i in range(MAX_CONDITION_CLAUSES + 1)
    ]
    with pytest.raises(ValueError, match="maximum of 32 clauses"):
        validate_policy_conditions(too_many_clauses)


# ---------------------------------------------------------------------------
# 2. Core PolicyEngine Evaluation & Masking
# ---------------------------------------------------------------------------


def test_policy_engine_regex_case_sensitivity_and_modes():
    """AC 4: Verify regex matching, ReDoS rejection, case sensitivity, and all/any/none trees."""
    regex_rule = {
        "type": "pattern",
        "expression": r"^inv_\d{4}\.pdf$",
        "target_path": "Finance/DatedInvoices",
        "priority": 90,
        "regex": True,
    }
    assert PolicyEngine.match_policy(regex_rule, "inv_2026.pdf", "", None) is True
    assert PolicyEngine.match_policy(regex_rule, "INV_2026.PDF", "", None) is True
    assert PolicyEngine.match_policy(regex_rule, "inv_26.pdf", "", None) is False

    # Case-sensitive rule on URGENT
    urgent_rule = {
        "type": "keyword",
        "expression": "URGENT",
        "target_path": "Priority/Urgent",
        "priority": 95,
        "case_sensitive": True,
    }
    assert PolicyEngine.match_policy(urgent_rule, "URGENT_notice.txt", "", None) is True
    assert (
        PolicyEngine.match_policy(urgent_rule, "urgent_notice.txt", "", None) is False
    )
    assert (
        PolicyEngine.match_policy(
            urgent_rule, "memo.txt", "This is URGENT action", None
        )
        is True
    )
    assert (
        PolicyEngine.match_policy(
            urgent_rule, "memo.txt", "this is urgent action", None
        )
        is False
    )

    # ReDoS pattern rejected when passed directly to PolicyEngine.match_policy / match_pattern_expression
    with pytest.raises(ValueError, match="nested quantifiers"):
        match_pattern_expression(r"(a+)+$", "aaaaab", regex=True)

    # 'none' mode (NOR) + path + starts_with + ends_with + any_text
    nor_rule = {
        "type": "pattern",
        "expression": "NONE(draft or temp)",
        "target_path": "Clean",
        "priority": 80,
        "match_mode": "none",
        "conditions": [
            {"field": "name", "operator": "starts_with", "value": "draft_"},
            {"field": "path", "operator": "contains", "value": "archive/"},
            {"field": "name", "operator": "ends_with", "value": ".tmp"},
        ],
    }
    assert PolicyEngine.match_policy(nor_rule, "incoming/report.pdf", "", None) is True
    assert (
        PolicyEngine.match_policy(nor_rule, "incoming/draft_v1.pdf", "", None) is False
    )
    assert PolicyEngine.match_policy(nor_rule, "archive/report.pdf", "", None) is False
    assert PolicyEngine.match_policy(nor_rule, "incoming/notes.tmp", "", None) is False


def test_is_masked_by_compound_and_flags():
    """Verify is_masked_by avoids false positives on distinct compound rules while catching true supersets."""
    broad_compound = {
        "type": "pattern",
        "expression": "ALL(ext==pdf)",
        "target_path": "AllPDFs",
        "priority": 90,
        "match_mode": "all",
        "conditions": [
            {"field": "extension", "operator": "equals", "value": "pdf"},
        ],
    }
    narrow_compound = {
        "type": "pattern",
        "expression": "ALL(ext==pdf, name contains invoice)",
        "target_path": "Invoices",
        "priority": 80,
        "match_mode": "all",
        "conditions": [
            {"field": "extension", "operator": "equals", "value": "pdf"},
            {"field": "name", "operator": "contains", "value": "invoice"},
        ],
    }
    other_compound = {
        "type": "pattern",
        "expression": "ALL(ext==docx, name contains invoice)",
        "target_path": "WordInvoices",
        "priority": 70,
        "match_mode": "all",
        "conditions": [
            {"field": "extension", "operator": "equals", "value": "docx"},
            {"field": "name", "operator": "contains", "value": "invoice"},
        ],
    }
    # is_masked_by(higher_rule, lower_rule) checks whether lower_rule is masked by higher_rule
    assert is_masked_by(broad_compound, narrow_compound) is True
    assert is_masked_by(narrow_compound, broad_compound) is False
    assert is_masked_by(broad_compound, other_compound) is False

    # Case-insensitive masks identical case-sensitive rule, but not vice versa
    ci_rule = {
        "type": "keyword",
        "expression": "urgent",
        "target_path": "A",
        "priority": 90,
        "case_sensitive": False,
    }
    cs_rule = {
        "type": "keyword",
        "expression": "URGENT",
        "target_path": "B",
        "priority": 80,
        "case_sensitive": True,
    }
    assert is_masked_by(ci_rule, cs_rule) is True
    assert is_masked_by(cs_rule, ci_rule) is False


# ---------------------------------------------------------------------------
# 3. MetadataPass, IncrementalAnalyzer, and DropZoneModal Integration
# ---------------------------------------------------------------------------


def test_metadata_pass_blocks_bypass_on_content_clause_and_analyzer_routes(tmp_path):
    """Req 5 & 6: MetadataPass skips bypass when rules inspect content; IncrementalAnalyzer evaluates compound policies before KEYWORD_RULES."""
    base_dir = tmp_path / "workspace"
    base_dir.mkdir()
    f_final = base_dir / "final_report.txt"
    f_final.write_text("Quarterly invoice #90210 total due $500", encoding="utf-8")
    f_draft = base_dir / "draft_report.txt"
    f_draft.write_text("Quarterly invoice #90211 draft preview", encoding="utf-8")

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    # Broad keyword rule that would misroute draft_report.txt if policies didn't run first or if exclusions were dropped
    settings.KEYWORD_RULES = {"report": "Generic/Reports"}
    settings.POLICIES = [
        {
            "type": "pattern",
            "expression": "ALL(ext==txt, content contains invoice, NOT name contains draft)",
            "target_path": "Finance/FinalInvoices",
            "priority": 90,
            "enabled": True,
            "match_mode": "all",
            "conditions": [
                {"field": "extension", "operator": "equals", "value": "txt"},
                {"field": "content", "operator": "contains", "value": "invoice"},
                {
                    "field": "name",
                    "operator": "contains",
                    "value": "draft",
                    "negate": True,
                },
            ],
        }
    ]

    db_worker = DBWorker()
    db = Database(tmp_path / "test.db", db_worker)
    try:
        # Even though "report" matches KEYWORD_RULES statically, MetadataPass MUST NOT bypass
        # supported .txt files because an active policy inspects content!
        bypassed = MetadataPass.run(
            str(base_dir),
            ["final_report.txt", "draft_report.txt"],
            settings,
            db,
        )
        assert bypassed == []

        # Simulate extracted text in DB and run IncrementalAnalyzer
        db.upsert_documents(
            [
                (
                    str(base_dir),
                    "final_report.txt",
                    "hash1",
                    f_final.read_text(encoding="utf-8"),
                ),
                (
                    str(base_dir),
                    "draft_report.txt",
                    "hash2",
                    f_draft.read_text(encoding="utf-8"),
                ),
            ]
        )

        analyzer = IncrementalAnalyzer(
            max_folders=3,
            stop_words={"the", "and"},
            db=db,
            model_path=None,
        )
        plan = analyzer.generate_sorting_plan(str(base_dir), runtime_settings=settings)
        plan_dict = plan.plan if hasattr(plan, "plan") else plan

        assert "final_report.txt" in plan_dict["Finance"]["FinalInvoices"]
        assert "draft_report.txt" in plan_dict["Generic"]["Reports"]
    finally:
        db_worker.stop()
        from app.core.db_conn import clear_connection_cache

        clear_connection_cache()


def test_drop_zone_is_supported_file_evaluates_compound_policy(tmp_path):
    """Req 5: DropZoneModal._is_supported_file routes compound policy checks through PolicyEngine."""
    from app.core.metadata import evaluate_static_file_routing

    good_bin = tmp_path / "fw_prod_v2.custombin"
    good_bin.write_bytes(b"\x00\x01\x02")
    excluded_bin = tmp_path / "fw_beta_v2.custombin"
    excluded_bin.write_bytes(b"\x00\x01\x02")

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.KEYWORD_RULES = {}
    settings.POLICIES = [
        {
            "type": "pattern",
            "expression": "ALL(ext==custombin, NOT beta)",
            "target_path": "Firmware/Prod",
            "priority": 80,
            "halting": True,
            "enabled": True,
            "match_mode": "all",
            "conditions": [
                {"field": "extension", "operator": "equals", "value": "custombin"},
                {
                    "field": "name",
                    "operator": "contains",
                    "value": "beta",
                    "negate": True,
                },
            ],
        }
    ]

    assert evaluate_static_file_routing(str(good_bin), settings) == (
        "Firmware/Prod",
        False,
        False,
    )
    assert evaluate_static_file_routing(str(excluded_bin), settings) == (
        None,
        False,
        True,
    )

    modal = DropZoneModal(settings=settings, base_dir=str(tmp_path))
    assert modal._is_supported_file(str(good_bin)) is True
    assert modal._is_supported_file(str(excluded_bin)) is False


# ---------------------------------------------------------------------------
# 4. External Importers (Hazel, DropIt, TagSpaces) & Fail-Closed Filters
# ---------------------------------------------------------------------------


def test_ac1_hazel_all_nested_and_exclusion_import(tmp_path):
    """AC 1 & Req 7: Hazel 'all' rule with Extension==pdf, Contents contains invoice, Name does not contain draft."""
    hazel_payload = {
        "Rules": [
            {
                "NSRuleName": "Final PDF Invoices",
                "NSRuleConditionMode": "all",
                "NSRuleConditions": [
                    {"target": "Extension", "operator": "is", "value": "pdf"},
                    {"target": "Contents", "operator": "contains", "value": "invoice"},
                    {
                        "target": "Name",
                        "operator": "does not contain",
                        "value": "draft",
                    },
                ],
                "NSRuleActions": [{"DestinationPath": "Finance/FinalInvoices"}],
            },
            {
                "NSRuleName": "Nested Urgent Or Regex",
                "NSRuleConditionMode": "all",
                "NSRuleConditions": [
                    {"target": "Extension", "operator": "is", "value": "pdf"},
                    {
                        "NSRuleConditionMode": "any",
                        "NSRuleConditions": [
                            {
                                "target": "Name",
                                "operator": "matches",
                                "value": r"^inv_\d{4}\.pdf$",
                                "regex": True,
                            },
                            {
                                "target": "Name",
                                "operator": "contains",
                                "value": "URGENT",
                                "caseSensitive": True,
                            },
                        ],
                    },
                ],
                "NSRuleActions": [{"DestinationPath": "Finance/Priority"}],
            },
        ]
    }
    hazel_path = tmp_path / "ac1.hazelrules"
    with open(hazel_path, "wb") as f:
        plistlib.dump(hazel_payload, f)

    res = HazelRuleImporter().parse_file(hazel_path)
    assert res.keyword_rules == {}
    assert len(res.policies) == 2

    inv_rule = next(
        p for p in res.policies if p["target_path"] == "Finance/FinalInvoices"
    )
    assert inv_rule["match_mode"] == "all"
    assert (
        PolicyEngine.match_policy(
            inv_rule, "final_invoice.pdf", "Attached invoice #42", None
        )
        is True
    )
    assert (
        PolicyEngine.match_policy(
            inv_rule, "draft_syllabus.pdf", "Attached invoice #42", None
        )
        is False
    )
    assert (
        PolicyEngine.match_policy(
            inv_rule, "final_syllabus.pdf", "Course syllabus overview", None
        )
        is False
    )

    nested_rule = next(
        p for p in res.policies if p["target_path"] == "Finance/Priority"
    )
    assert PolicyEngine.match_policy(nested_rule, "inv_2026.pdf", "", None) is True
    assert PolicyEngine.match_policy(nested_rule, "URGENT_bill.pdf", "", None) is True
    assert PolicyEngine.match_policy(nested_rule, "urgent_bill.pdf", "", None) is False


def test_ac2_dropit_xml_exclude_and_slash_exclusion_import(tmp_path):
    """AC 2 & Req 8: DropIt XML <Exclude>*draft*</Exclude>, *.jpg/sea* slash exclusions, # exclusions, and regex."""
    xml_content = """<?xml version="1.0" encoding="UTF-8"?>
    <Profile>
        <Association>
            <Name>Clean PDFs</Name>
            <Pattern>*.pdf</Pattern>
            <Exclude>*draft*</Exclude>
            <Destination>Docs/FinalPDFs</Destination>
            <Action>Move</Action>
        </Association>
        <Association>
            <Name>Landscapes Without Sea</Name>
            <Pattern>*.jpg/sea*</Pattern>
            <Destination>Photos/Landscapes</Destination>
            <Action>Move</Action>
        </Association>
        <Association>
            <Name>Regex Invoices</Name>
            <Pattern>/^inv_\\d{4}\\.pdf$/</Pattern>
            <Destination>Finance/RegexInvoices</Destination>
            <Action>Move</Action>
        </Association>
    </Profile>
    """
    xml_file = tmp_path / "dropit_ac2.xml"
    xml_file.write_text(xml_content, encoding="utf-8")

    res = DropItRuleImporter().parse_file(xml_file)
    assert res.keyword_rules == {}
    assert len(res.policies) == 3

    pdf_rule = next(p for p in res.policies if p["target_path"] == "Docs/FinalPDFs")
    assert PolicyEngine.match_policy(pdf_rule, "report_v1.pdf", "", None) is True
    assert PolicyEngine.match_policy(pdf_rule, "draft_v1.pdf", "", None) is False

    jpg_rule = next(p for p in res.policies if p["target_path"] == "Photos/Landscapes")
    assert PolicyEngine.match_policy(jpg_rule, "mountain.jpg", "", None) is True
    assert PolicyEngine.match_policy(jpg_rule, "forest.jpg", "", None) is True
    assert PolicyEngine.match_policy(jpg_rule, "seaside.jpg", "", None) is False

    rx_rule = next(
        p for p in res.policies if p["target_path"] == "Finance/RegexInvoices"
    )
    assert PolicyEngine.match_policy(rx_rule, "inv_2026.pdf", "", None) is True
    assert PolicyEngine.match_policy(rx_rule, "inv_99.pdf", "", None) is False


def test_ac3_tagspaces_smart_rule_compound_and_negation(tmp_path):
    """AC 3 & Req 9: TagSpaces smart rule with extension==pdf, name contains contract, and negated draft."""
    ts_payload = {
        "smartRules": [
            {
                "title": "Signed Contracts",
                "destination": "Legal/Contracts",
                "conditions": [
                    {"field": "extension", "operator": "equals", "value": "pdf"},
                    {"field": "name", "operator": "contains", "value": "contract"},
                    {
                        "field": "name",
                        "operator": "does not contain",
                        "value": "draft",
                    },
                ],
            }
        ]
    }
    ts_file = tmp_path / "taggroups.json"
    ts_file.write_text(json.dumps(ts_payload), encoding="utf-8")

    res = TagSpacesRuleImporter().parse_file(ts_file)
    assert res.keyword_rules == {}
    assert len(res.policies) == 1

    rule = res.policies[0]
    assert rule["match_mode"] == "all"
    assert PolicyEngine.match_policy(rule, "vendor_contract.pdf", "", None) is True
    assert PolicyEngine.match_policy(rule, "draft_notes.pdf", "", None) is False
    assert PolicyEngine.match_policy(rule, "draft_contract.pdf", "", None) is False
    assert PolicyEngine.match_policy(rule, "vendor_contract.docx", "", None) is False


def test_req10_importers_fail_closed_on_size_date_and_xattr_filters(tmp_path):
    """Req 10 & Scenario 4: Importers skip the entire rule and emit warnings on size, date, or xattr filters."""
    # 1. Hazel with size, date, and extended attribute rules paired with *.pdf
    hazel_data = {
        "Rules": [
            {
                "NSRuleName": "Large PDFs",
                "NSRuleConditionMode": "all",
                "NSRuleConditions": [
                    {"target": "Extension", "operator": "is", "value": "pdf"},
                    {"target": "Size", "operator": "greater than", "value": "10MB"},
                ],
                "NSRuleActions": [{"DestinationPath": "LargePDFs"}],
            },
            {
                "NSRuleName": "Downloaded Xattr PDFs",
                "NSRuleConditionMode": "none",
                "NSRuleConditions": [
                    {"target": "Extension", "operator": "is", "value": "pdf"},
                    {
                        "target": "kMDItemWhereFroms",
                        "operator": "contains",
                        "value": "example.com",
                    },
                ],
                "NSRuleActions": [{"DestinationPath": "UntrustedPDFs"}],
            },
        ]
    }
    hz_file = tmp_path / "fail_closed.hazelrules"
    with open(hz_file, "wb") as f:
        plistlib.dump(hazel_data, f)
    hz_res = HazelRuleImporter().parse_file(hz_file)
    assert hz_res.keyword_rules == {}
    assert hz_res.policies == []
    assert len(hz_res.warnings) >= 2

    # 2. DropIt XML pairing *.pdf with <Size> filter
    dropit_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <Profile>
        <Association>
            <Name>Old Large PDFs</Name>
            <Pattern>*.pdf</Pattern>
            <Size>&gt;5MB</Size>
            <Destination>Archive/LargePDFs</Destination>
            <Action>Move</Action>
        </Association>
    </Profile>
    """
    di_file = tmp_path / "fail_closed.xml"
    di_file.write_text(dropit_xml, encoding="utf-8")
    di_res = DropItRuleImporter().parse_file(di_file)
    assert di_res.keyword_rules == {}
    assert di_res.policies == []
    assert any("size" in w.lower() for w in di_res.warnings)

    # 3. TagSpaces smart rule pairing *.pdf with modified date filter
    ts_data = {
        "smartRules": [
            {
                "title": "Recent PDFs",
                "destination": "Recent/PDFs",
                "conditions": [
                    {"field": "extension", "operator": "equals", "value": "pdf"},
                    {"field": "dateModified", "operator": ">", "value": "2026-01-01"},
                ],
            }
        ]
    }
    ts_file = tmp_path / "fail_closed_ts.json"
    ts_file.write_text(json.dumps(ts_data), encoding="utf-8")
    ts_res = TagSpacesRuleImporter().parse_file(ts_file)
    assert ts_res.keyword_rules == {}
    assert ts_res.policies == []
    assert any("date" in w.lower() for w in ts_res.warnings)


# ---------------------------------------------------------------------------
# 5. Conflict Detection, Export Round-Trip, CLI, and TUI Workflow (AC 5)
# ---------------------------------------------------------------------------


def test_conflict_analyzer_and_export_round_trip(tmp_path):
    """Req 11: Verify RuleConflictAnalyzer, Sortify JSON, Hazel, and DropIt INI round-trip compound rules."""
    compound_rule = {
        "type": "pattern",
        "expression": "ALL(extension == 'jpg', NOT name glob 'sea*')",
        "target_path": "Photos/Mountains",
        "priority": 80,
        "enabled": True,
        "match_mode": "all",
        "conditions": [
            {"field": "extension", "operator": "equals", "value": "jpg"},
            {
                "field": "name",
                "operator": "glob",
                "value": "sea*",
                "negate": True,
            },
        ],
    }

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.KEYWORD_RULES = {}
    settings.POLICIES = [compound_rule]

    # Conflict detection with different target_path for the exact same condition tree
    conflicting_import = ImportResult(
        policies=[
            {
                **compound_rule,
                "target_path": "Photos/Outdoor",
            }
        ]
    )
    conflicts = RuleConflictAnalyzer.analyze(conflicting_import, settings)
    assert len(conflicts) == 1
    assert conflicts[0].existing_target == "Photos/Mountains"
    assert conflicts[0].imported_target == "Photos/Outdoor"

    # 1. Sortify JSON round-trip
    json_out = tmp_path / "export.json"
    export_active_rules(settings, "sortify_json", json_out)
    reimp_json = SortifyRuleImporter().parse_file(json_out)
    assert len(reimp_json.policies) == 1
    assert reimp_json.policies[0]["match_mode"] == "all"
    assert reimp_json.policies[0]["conditions"] == compound_rule["conditions"]

    # 2. Hazel round-trip
    hazel_out = tmp_path / "export.hazelrules"
    export_active_rules(settings, "hazel", hazel_out)
    reimp_hz = HazelRuleImporter().parse_file(hazel_out)
    assert len(reimp_hz.policies) == 1
    hz_pol = reimp_hz.policies[0]
    assert PolicyEngine.match_policy(hz_pol, "mountain.jpg", "", None) is True
    assert PolicyEngine.match_policy(hz_pol, "seaside.jpg", "", None) is False

    # 3. DropIt INI round-trip
    ini_out = tmp_path / "export.ini"
    export_active_rules(settings, "dropit_ini", ini_out)
    reimp_di = DropItRuleImporter().parse_file(ini_out)
    assert len(reimp_di.policies) == 1
    di_pol = reimp_di.policies[0]
    assert PolicyEngine.match_policy(di_pol, "mountain.jpg", "", None) is True
    assert PolicyEngine.match_policy(di_pol, "seaside.jpg", "", None) is False


@pytest.mark.anyio
async def test_ac5_tui_author_edit_toggle_export_and_cli_reimport(tmp_path):
    """AC 5 & Req 12: Create, edit, toggle, export, and CLI re-import compound rules via RuleEditModal & ActiveRuleManagerModal."""
    settings_path = tmp_path / "tui_settings.json"
    settings = AppSettings(filepath=str(settings_path))
    settings.KEYWORD_RULES = {}
    settings.POLICIES = []

    manager = ActiveRuleManagerModal(settings)
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test(size=(110, 38)) as pilot:
        await pilot.pause()
        app.push_screen(manager)
        await pilot.pause()

        # 1. Open RuleEditModal via Add Rule
        manager.action_add_rule()
        await pilot.pause()
        assert isinstance(app.screen, RuleEditModal)
        editor = app.screen

        # Switch match mode to "all" (compound AND)
        mode_sel = editor.query_one("#sel-rule-match-mode", Select)
        mode_sel.value = "all"
        await pilot.pause()

        # Verify first clause row is visible and configure: extension equals pdf
        rows = list(editor.query(_PolicyClauseRow))
        assert len(rows) == 1
        r0 = rows[0].row_index
        editor.query_one(f"#sel-clause-field-{r0}", Select).value = "extension"
        editor.query_one(f"#sel-clause-op-{r0}", Select).value = "equals"
        editor.query_one(f"#input-clause-val-{r0}", Input).value = "pdf"

        # Add second clause: name contains invoice
        r1_widget = editor.add_clause_row(
            {"field": "name", "operator": "contains", "value": "invoice"}
        )
        # Add third clause: name contains draft (negated)
        r2_widget = editor.add_clause_row(
            {
                "field": "name",
                "operator": "contains",
                "value": "draft",
                "negate": True,
            }
        )
        await pilot.pause()
        assert r1_widget is not None and r2_widget is not None

        # Verify ReDoS validation error blocks saving before fixing
        bad_row = editor.add_clause_row(
            {"field": "name", "operator": "regex", "value": "(a+)+$"}
        )
        await pilot.pause()
        editor.query_one("#input-rule-target", Input).value = "Finance/CompoundInvoices"
        editor.action_save()
        await pilot.pause()
        err_lbl = editor.query_one("#lbl-rule-error", Label)
        assert "nested quantifiers" in str(err_lbl.render()).lower()
        assert isinstance(app.screen, RuleEditModal)

        # Remove bad clause and save valid compound policy
        assert bad_row is not None
        editor.remove_last_clause_row()
        await pilot.pause()
        editor.action_save()
        await pilot.pause()

        assert isinstance(app.screen, ActiveRuleManagerModal)
        assert len(settings.POLICIES) == 1
        saved_pol = settings.POLICIES[0]
        assert saved_pol["match_mode"] == "all"
        assert saved_pol["target_path"] == "Finance/CompoundInvoices"
        assert len(saved_pol["conditions"]) == 3

        # 2. Edit the compound policy via ActiveRuleManagerModal
        table = manager.query_one("#rules-table", DataTable)
        table.move_cursor(row=0)
        manager.action_edit_rule()
        await pilot.pause()
        assert isinstance(app.screen, RuleEditModal)
        edit_modal = app.screen
        assert edit_modal.query_one("#sel-rule-match-mode", Select).value == "all"
        edit_modal.query_one(
            "#input-rule-target", Input
        ).value = "Finance/VerifiedInvoices"
        edit_modal.action_save()
        await pilot.pause()
        assert settings.POLICIES[0]["target_path"] == "Finance/VerifiedInvoices"

        # 3. Toggle status Disabled -> Active
        manager.action_toggle_rule()
        await pilot.pause()
        assert settings.POLICIES[0]["enabled"] is False
        manager.action_toggle_rule()
        await pilot.pause()
        assert settings.POLICIES[0]["enabled"] is True
        settings.flush()

    # 4. Export and re-import via `smart-autosorter rules` CLI
    exported_json = tmp_path / "cli_compound_export.json"
    res_exp = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.main",
            "rules",
            "export",
            str(exported_json),
            "--settings-file",
            str(settings_path),
            "--format",
            "sortify_json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_exp.returncode == 0, res_exp.stderr

    dest_settings_path = tmp_path / "reimported_settings.json"
    res_imp = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.main",
            "rules",
            "import",
            str(exported_json),
            "--settings-file",
            str(dest_settings_path),
            "--enable",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_imp.returncode == 0, res_imp.stderr
    assert "[mode=all]" in res_imp.stdout

    reloaded = AppSettings(filepath=str(dest_settings_path))
    assert len(reloaded.POLICIES) == 1
    reimp_pol = reloaded.POLICIES[0]
    assert reimp_pol["match_mode"] == "all"
    assert reimp_pol["target_path"] == "Finance/VerifiedInvoices"
    assert PolicyEngine.match_policy(reimp_pol, "final_invoice.pdf", "", None) is True
    assert PolicyEngine.match_policy(reimp_pol, "draft_invoice.pdf", "", None) is False


@pytest.mark.anyio
async def test_compound_policy_coexistence_with_rename_templates_and_live_preview(
    tmp_path,
):
    """Verify compound rules coexist cleanly with #1193 rename_pattern/{year} templates and #1190 live sample preview + a11y."""
    workspace = tmp_path / "live_ws"
    workspace.mkdir()
    (workspace / "invoice_final.txt").write_text(
        "Invoice #8841 due immediately", encoding="utf-8"
    )
    (workspace / "invoice_draft.txt").write_text(
        "Invoice #8842 draft copy", encoding="utf-8"
    )
    (workspace / "notes.md").write_text("Team meeting notes", encoding="utf-8")

    settings = AppSettings(filepath=str(tmp_path / "coexist_settings.json"))
    settings.KEYWORD_RULES = {}
    settings.POLICIES = []

    manager = ActiveRuleManagerModal(settings)
    app = AutoSorterTUI(settings=settings, base_dir=str(workspace))

    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        app.push_screen(manager)
        await pilot.pause()

        manager.action_add_rule()
        await pilot.pause()
        assert isinstance(app.screen, RuleEditModal)
        editor = app.screen

        # Switch to compound AND mode and configure extension==txt AND content contains invoice
        editor.query_one("#sel-rule-match-mode", Select).value = "all"
        await pilot.pause()

        rows = list(editor.query(_PolicyClauseRow))
        r0 = rows[0].row_index
        editor.query_one(f"#sel-clause-field-{r0}", Select).value = "extension"
        editor.query_one(f"#sel-clause-op-{r0}", Select).value = "equals"
        editor.query_one(f"#input-clause-val-{r0}", Input).value = "txt"
        editor.add_clause_row(
            {"field": "content", "operator": "contains", "value": "invoice"}
        )
        await pilot.pause()

        # Select invoice_draft.txt as sample file — initially matches 2 of 3 workspace files
        sample_sel = editor.query_one("#select-sample-file", Select)
        draft_opt = next(
            item_id
            for item_id, _src, rel_p in editor._workspace_items
            if rel_p == "invoice_draft.txt"
        )
        sample_sel.value = draft_opt
        await pilot.pause()

        preview_lbl = editor.query_one("#lbl-rule-preview", Label)
        preview_before = str(preview_lbl.render())
        assert "MATCH on 'invoice_draft.txt'" in preview_before
        assert "Workspace matches: 2 of 3 file(s)" in preview_before

        # Add negated 'draft' clause -> live preview updates immediately to NO MATCH and 1 of 3 files!
        editor.add_clause_row(
            {
                "field": "name",
                "operator": "contains",
                "value": "draft",
                "negate": True,
            }
        )
        editor._refresh_preview(announce_change=False)
        await pilot.pause()
        preview_after = str(preview_lbl.render())
        assert "NO MATCH on 'invoice_draft.txt'" in preview_after
        assert "Workspace matches: 1 of 3 file(s)" in preview_after

        # Verify #1190 accessibility audit passes with compound clause rows mounted
        a11y_report = editor.audit_a11y_compliance()
        assert a11y_report["violations"] == []

        # Combine compound rule with #1193 {year}/{month} target template and rename_pattern
        editor.query_one("#input-rule-target", Input).value = "Finance/{year}/{month}"
        editor.query_one(
            "#input-rule-rename-pattern", Input
        ).value = "{date}_{stem}{extension}"
        editor.action_save()
        await pilot.pause()

        assert isinstance(app.screen, ActiveRuleManagerModal)
        assert len(settings.POLICIES) == 1
        saved = settings.POLICIES[0]
        assert saved["match_mode"] == "all"
        assert saved["target_path"] == "Finance/{year}/{month}"
        assert saved["rename_pattern"] == "{date}_{stem}{extension}"

    # Verify PolicyEngine.evaluate_policies preserves rename_pattern alongside compound match
    eval_res = PolicyEngine.evaluate_policies(
        "invoice_final.txt",
        "Invoice #8841 due immediately",
        None,
        settings.POLICIES,
        abs_file_path=str(workspace / "invoice_final.txt"),
    )
    assert eval_res is not None
    assert eval_res.halt_evaluation is False
    assert eval_res.target_path == "Finance/{year}/{month}"
    assert eval_res.rename_pattern == "{date}_{stem}{extension}"

    # Verify Hazel & Sortify JSON export/import round-trip both compound conditions and rename_pattern
    hz_path = tmp_path / "coexist.hazelrules"
    export_active_rules(settings, "hazel", hz_path)
    hz_reimp = HazelRuleImporter().parse_file(hz_path)
    assert len(hz_reimp.policies) == 1
    assert hz_reimp.policies[0]["match_mode"] == "all"
    assert hz_reimp.policies[0]["rename_pattern"] == "{date}_{stem}{extension}"
