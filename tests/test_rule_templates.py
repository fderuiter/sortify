"""Unit tests for onboarding starter rule template gallery and policy integration."""

import tempfile

from app.config import AppSettings
from app.core.policy_engine import PolicyEngine
from app.core.rule_templates import (
    apply_starter_templates,
    get_starter_template_by_id,
    get_starter_templates,
)


def test_get_starter_templates():
    """Verify built-in starter rule template packs exist and have required fields."""
    templates = get_starter_templates()
    assert len(templates) >= 6

    ids = [t.id for t in templates]
    assert "financial_tax" in ids
    assert "medical_health" in ids
    assert "legal_contracts" in ids
    assert "personal_admin" in ids
    assert "work_projects" in ids
    assert "office_hr" in ids

    for pack in templates:
        assert pack.id
        assert pack.title
        assert pack.description
        assert isinstance(pack.keyword_rules, dict)
        assert isinstance(pack.policies, list)


def test_get_starter_template_by_id():
    """Verify looking up a starter template by ID."""
    fin = get_starter_template_by_id("financial_tax")
    assert fin is not None
    assert fin.title == "Financial & Tax Documents"
    assert "invoice" in fin.keyword_rules

    none_pack = get_starter_template_by_id("non_existent_pack")
    assert none_pack is None


def test_apply_starter_templates_fresh():
    """Verify applying starter template packs populates settings."""
    with tempfile.NamedTemporaryFile(suffix=".json") as f:
        settings = AppSettings(filepath=f.name)
        assert getattr(settings, "KEYWORD_RULES", {}) == {}
        assert getattr(settings, "POLICIES", []) == []

        kw, pol = apply_starter_templates(
            settings, ["financial_tax", "legal_contracts"]
        )

        assert "invoice" in kw
        assert "tax" in kw
        assert "contract" in kw
        assert "nda" in kw

        assert len(pol) > 0
        policy_exprs = [p["expression"] for p in pol]
        assert "confidential tax" in policy_exprs
        assert "confidential agreement" in policy_exprs


def test_apply_starter_templates_preserves_custom_rules():
    """Verify custom rules are never overwritten by template application."""
    with tempfile.NamedTemporaryFile(suffix=".json") as f:
        settings = AppSettings(filepath=f.name)
        # Set custom rule
        settings.KEYWORD_RULES = {"invoice": "MyCustom/Invoices"}
        settings.POLICIES = [
            {
                "type": "keyword",
                "expression": "confidential tax",
                "target_path": "MyCustom/ConfidentialTax",
                "priority": 100,
                "halting": False,
            }
        ]

        kw, pol = apply_starter_templates(settings, ["financial_tax"])

        # Custom invoice rule must remain unchanged
        assert kw["invoice"] == "MyCustom/Invoices"
        # New keywords from financial_tax should be added
        assert kw["receipt"] == "Financial/Receipts"
        assert kw["tax"] == "Taxes/Returns"

        # Policy with same (type, expression) should NOT be duplicated
        matching_policies = [p for p in pol if p["expression"] == "confidential tax"]
        assert len(matching_policies) == 1
        assert matching_policies[0]["target_path"] == "MyCustom/ConfidentialTax"


def test_policy_engine_routing_with_starter_templates():
    """Verify applied starter template policies filter and route sample files correctly."""
    with tempfile.NamedTemporaryFile(suffix=".json") as f:
        settings = AppSettings(filepath=f.name)
        apply_starter_templates(settings, ["financial_tax", "legal_contracts"])

        policies = getattr(settings, "POLICIES", [])

        # Match confidential tax document against policy engine
        file_path = "/workspace/confidential_tax_return_2025.pdf"
        doc_text = "Internal IRS return record confidential tax details"
        matched = PolicyEngine.evaluate_policies(file_path, doc_text, None, policies)
        assert matched is not None
        assert matched.target_path == "Taxes/Confidential"

        # Match NDA agreement document against policy engine
        file_path2 = "/workspace/vendor_confidential_agreement.pdf"
        doc_text2 = "Non disclosure confidential agreement between parties"
        matched2 = PolicyEngine.evaluate_policies(file_path2, doc_text2, None, policies)
        assert matched2 is not None
        assert matched2.target_path == "Legal/Confidential"
