"""Unit tests for rule export functionality and bidirectional format compatibility."""

import json
import plistlib
from unittest.mock import patch

import pytest

from app.config import AppSettings
from app.core.rule_importers import (
    DropItRuleImporter,
    HazelRuleImporter,
    SortifyRuleImporter,
    get_rule_importer,
)
from app.core.rule_templates import export_active_rules


@pytest.fixture
def populated_settings(tmp_path):
    """Provide an AppSettings instance populated with keyword rules and policies."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.KEYWORD_RULES = {
        "invoice": "Financial/Invoices",
        "receipt": "Financial/Receipts",
        "tax": "Taxes/Returns",
    }
    settings.POLICIES = [
        {
            "type": "keyword",
            "expression": "confidential tax",
            "target_path": "Taxes/Confidential",
            "priority": 90,
            "halting": False,
        },
        {
            "type": "pattern",
            "expression": "*.pdf",
            "target_path": "Documents/PDFs",
            "priority": 75,
            "halting": False,
        },
    ]
    return settings


def test_export_sortify_json(populated_settings, tmp_path):
    """Verify exporting active rules to Sortify JSON produces valid, complete JSON."""
    out_file = tmp_path / "rules.json"
    result_path = export_active_rules(populated_settings, "sortify_json", out_file)

    assert result_path == out_file
    assert out_file.exists()

    with open(out_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["format"] == "sortify"
    assert "KEYWORD_RULES" in data
    assert "POLICIES" in data
    assert data["KEYWORD_RULES"] == populated_settings.KEYWORD_RULES
    assert len(data["POLICIES"]) == len(populated_settings.POLICIES)


def test_export_sortify_json_bidirectional_roundtrip(populated_settings, tmp_path):
    """Verify exported Sortify JSON can be re-imported back without data loss."""
    out_file = tmp_path / "backup_rules.json"
    export_active_rules(populated_settings, "sortify_json", out_file)

    importer = get_rule_importer(out_file)
    assert isinstance(importer, SortifyRuleImporter)

    res = importer.parse_file(out_file)
    assert res.source_format == "Sortify JSON"
    assert res.keyword_rules == {
        k.lower(): v for k, v in populated_settings.KEYWORD_RULES.items()
    }
    assert len(res.policies) == len(populated_settings.POLICIES)

    target_settings = AppSettings(filepath=str(tmp_path / "target_settings.json"))
    kw, pol = res.merge_into_settings(target_settings)
    assert kw == populated_settings.KEYWORD_RULES
    assert len(pol) == len(populated_settings.POLICIES)


def test_export_hazel_format(populated_settings, tmp_path):
    """Verify exporting active rules to Hazel format produces valid Apple property list (.hazelrules)."""
    out_file = tmp_path / "exported.hazelrules"
    result_path = export_active_rules(populated_settings, "hazel", out_file)

    assert result_path == out_file
    assert out_file.exists()

    with open(out_file, "rb") as f:
        plist_data = plistlib.load(f)

    assert "Rules" in plist_data
    assert isinstance(plist_data["Rules"], list)
    assert len(plist_data["Rules"]) == len(populated_settings.KEYWORD_RULES) + len(
        populated_settings.POLICIES
    )

    for rule in plist_data["Rules"]:
        assert "NSRuleName" in rule
        assert "NSRuleConditions" in rule
        assert "NSRuleActions" in rule
        assert len(rule["NSRuleActions"]) > 0
        assert "DestinationPath" in rule["NSRuleActions"][0]


def test_export_hazel_bidirectional_roundtrip(populated_settings, tmp_path):
    """Verify exported Hazel .hazelrules can be re-imported back without data loss."""
    out_file = tmp_path / "exported.hazelrules"
    export_active_rules(populated_settings, "hazel", out_file)

    importer = get_rule_importer(out_file)
    assert isinstance(importer, HazelRuleImporter)

    res = importer.parse_file(out_file)
    for kw, target in populated_settings.KEYWORD_RULES.items():
        assert kw.lower() in res.keyword_rules
        assert res.keyword_rules[kw.lower()] == target

    confidential_policy = next(
        (p for p in res.policies if p["expression"] == "confidential tax"), None
    )
    assert confidential_policy is not None
    assert confidential_policy["target_path"] == "Taxes/Confidential"

    pdf_policy = next(
        (p for p in res.policies if p["expression"] in ("*.pdf", "pdf")), None
    )
    assert pdf_policy is not None
    assert pdf_policy["target_path"] == "Documents/PDFs"


def test_export_dropit_format(populated_settings, tmp_path):
    """Verify exporting active rules to DropIt format produces valid .ini configuration."""
    out_file = tmp_path / "exported.ini"
    result_path = export_active_rules(populated_settings, "dropit_ini", out_file)

    assert result_path == out_file
    assert out_file.exists()

    content = out_file.read_text(encoding="utf-8")
    assert "[DropIt]" in content
    assert "Profile = Sortify Exported Rules" in content
    assert "[Association_1]" in content
    assert "Rules = invoice" in content
    assert "Destination = Financial/Invoices" in content


def test_export_dropit_bidirectional_roundtrip(populated_settings, tmp_path):
    """Verify exported DropIt .ini can be re-imported back without data loss."""
    out_file = tmp_path / "exported.ini"
    export_active_rules(populated_settings, "dropit_ini", out_file)

    importer = get_rule_importer(out_file)
    assert isinstance(importer, DropItRuleImporter)

    res = importer.parse_file(out_file)
    assert res.source_format == "DropIt INI Profile"
    for kw, target in populated_settings.KEYWORD_RULES.items():
        assert kw.lower() in res.keyword_rules
        assert res.keyword_rules[kw.lower()] == target

    confidential_policy = next(
        (p for p in res.policies if p["expression"] == "confidential tax"), None
    )
    assert confidential_policy is not None
    assert confidential_policy["target_path"] == "Taxes/Confidential"

    pdf_policy = next((p for p in res.policies if p["expression"] == "*.pdf"), None)
    assert pdf_policy is not None
    assert pdf_policy["target_path"] == "Documents/PDFs"


def test_export_preserves_active_settings(populated_settings, tmp_path):
    """Verify export operations never mutate or delete active user settings."""
    original_kw = dict(populated_settings.KEYWORD_RULES)
    original_pol = [dict(p) for p in populated_settings.POLICIES]

    export_active_rules(populated_settings, "sortify_json", tmp_path / "r.json")
    export_active_rules(populated_settings, "hazel", tmp_path / "r.hazelrules")
    export_active_rules(populated_settings, "dropit_ini", tmp_path / "r.ini")

    assert populated_settings.KEYWORD_RULES == original_kw
    assert populated_settings.POLICIES == original_pol


def test_export_empty_rules(tmp_path):
    """Verify exporting settings with no rules succeeds cleanly."""
    settings = AppSettings(filepath=str(tmp_path / "empty_settings.json"))
    settings.KEYWORD_RULES = {}
    settings.POLICIES = []

    p_json = export_active_rules(settings, "sortify_json", tmp_path / "empty.json")
    assert p_json.exists()

    p_hazel = export_active_rules(settings, "hazel", tmp_path / "empty.hazelrules")
    assert p_hazel.exists()

    p_ini = export_active_rules(settings, "dropit_ini", tmp_path / "empty.ini")
    assert p_ini.exists()


def test_export_invalid_destination_paths(populated_settings, tmp_path):
    """Verify validation and directory traversal prevention on destination file paths."""
    with pytest.raises(ValueError, match="Destination file path cannot be empty"):
        export_active_rules(populated_settings, "sortify_json", "")

    with pytest.raises(ValueError, match="Destination file path cannot be empty"):
        export_active_rules(populated_settings, "sortify_json", "   ")

    with pytest.raises(ValueError, match="contains null characters"):
        export_active_rules(populated_settings, "sortify_json", "/tmp/rules\0.json")

    with pytest.raises(ValueError, match="Directory traversal"):
        export_active_rules(populated_settings, "sortify_json", "../secret_rules.json")

    with pytest.raises(ValueError, match="Directory traversal"):
        export_active_rules(
            populated_settings, "sortify_json", "sub/../../etc/rules.json"
        )

    # Destination is an existing directory
    existing_dir = tmp_path / "export_dir"
    existing_dir.mkdir()
    with pytest.raises(ValueError, match="is an existing directory"):
        export_active_rules(populated_settings, "sortify_json", existing_dir)


def test_export_unsupported_format(populated_settings, tmp_path):
    """Verify ValueError raised when requesting an unknown target format."""
    with pytest.raises(ValueError, match="Unsupported export format"):
        export_active_rules(populated_settings, "unsupported_xyz", tmp_path / "r.xyz")


def test_export_unwritable_destination(populated_settings, tmp_path):
    """Verify OSError raised when export destination directory is unwritable."""
    read_only_dir = tmp_path / "readonly_dir"
    read_only_dir.mkdir()

    with patch("os.access", return_value=False):
        with pytest.raises(OSError, match="not writable"):
            export_active_rules(
                populated_settings, "sortify_json", read_only_dir / "test.json"
            )
