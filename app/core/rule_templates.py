"""Starter rule template gallery and rule preset integration module.

Provides built-in starter rule template packs covering common document workflows
and safe merging utilities that preserve custom user rules.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class StarterTemplatePack:
    """Data container for a starter rule template pack."""

    id: str
    title: str
    description: str
    keyword_rules: Dict[str, str] = field(default_factory=dict)
    policies: List[Dict[str, Any]] = field(default_factory=list)
    icon: str = "📂"


BUILTIN_STARTER_TEMPLATES: List[StarterTemplatePack] = [
    StarterTemplatePack(
        id="financial_tax",
        title="Financial & Tax Documents",
        description="Automatically route invoices, receipts, tax returns, W-2s, and bank statements.",
        icon="💰",
        keyword_rules={
            "invoice": "Financial/Invoices",
            "receipt": "Financial/Receipts",
            "tax": "Taxes/Returns",
            "w2": "Taxes/W2",
            "statement": "Financial/Statements",
        },
        policies=[
            {
                "type": "keyword",
                "expression": "confidential tax",
                "target_path": "Taxes/Confidential",
                "priority": 90,
                "halting": False,
            },
            {
                "type": "keyword",
                "expression": "1099",
                "target_path": "Taxes/1099",
                "priority": 85,
                "halting": False,
            },
        ],
    ),
    StarterTemplatePack(
        id="medical_health",
        title="Medical & Health Records",
        description="Organize medical bills, prescription records, lab results, and insurance claims.",
        icon="🏥",
        keyword_rules={
            "prescription": "Medical/Prescriptions",
            "lab_result": "Medical/Lab_Results",
            "medical_bill": "Medical/Bills",
        },
        policies=[
            {
                "type": "keyword",
                "expression": "insurance claim",
                "target_path": "Medical/Insurance_Claims",
                "priority": 80,
                "halting": False,
            },
        ],
    ),
    StarterTemplatePack(
        id="legal_contracts",
        title="Legal & Contracts",
        description="Sort legal agreements, contracts, NDAs, leases, and court filings.",
        icon="⚖️",
        keyword_rules={
            "contract": "Legal/Contracts",
            "nda": "Legal/NDAs",
            "lease": "Legal/Leases",
        },
        policies=[
            {
                "type": "keyword",
                "expression": "confidential agreement",
                "target_path": "Legal/Confidential",
                "priority": 95,
                "halting": False,
            },
        ],
    ),
    StarterTemplatePack(
        id="personal_admin",
        title="Personal Admin & Utilities",
        description="Organize utility bills, passports, warranties, and vehicle registrations.",
        icon="📑",
        keyword_rules={
            "utility": "Personal/Utilities",
            "passport": "Personal/Identity",
            "warranty": "Personal/Warranties",
        },
        policies=[
            {
                "type": "keyword",
                "expression": "registration",
                "target_path": "Personal/Vehicles",
                "priority": 70,
                "halting": False,
            },
        ],
    ),
    StarterTemplatePack(
        id="work_projects",
        title="Work & Projects",
        description="Route project plans, meeting notes, specifications, and quarterly reports.",
        icon="💼",
        keyword_rules={
            "project_plan": "Work/Projects",
            "meeting_notes": "Work/Meetings",
            "specification": "Work/Specs",
        },
        policies=[
            {
                "type": "keyword",
                "expression": "quarterly report",
                "target_path": "Work/Reports",
                "priority": 75,
                "halting": False,
            },
        ],
    ),
    StarterTemplatePack(
        id="office_hr",
        title="Office & HR Records",
        description="Organize resumes, performance reviews, and employee handbooks.",
        icon="👥",
        keyword_rules={
            "resume": "HR/Resumes",
            "performance_review": "HR/Reviews",
        },
        policies=[
            {
                "type": "keyword",
                "expression": "employee handbook",
                "target_path": "HR/Handbooks",
                "priority": 65,
                "halting": False,
            },
        ],
    ),
]


def get_starter_templates() -> List[StarterTemplatePack]:
    """Return list of built-in starter rule template packs."""
    return list(BUILTIN_STARTER_TEMPLATES)


def get_starter_template_by_id(pack_id: str) -> Optional[StarterTemplatePack]:
    """Find a starter template pack by unique ID."""
    for pack in BUILTIN_STARTER_TEMPLATES:
        if pack.id == pack_id:
            return pack
    return None


def apply_starter_templates(
    settings: Any, pack_ids: List[str]
) -> tuple[Dict[str, str], List[Dict[str, Any]]]:
    """Merge selected starter template packs into settings without overwriting existing custom rules.

    Args:
        settings: Application settings object (`AppSettings`).
        pack_ids: List of template pack IDs to apply.

    Returns
    -------
        Tuple of updated (KEYWORD_RULES dict, POLICIES list).
    """
    existing_keywords = dict(getattr(settings, "KEYWORD_RULES", {}) or {})
    existing_policies = list(getattr(settings, "POLICIES", []) or [])

    new_keywords = dict(existing_keywords)
    new_policies = [dict(p) for p in existing_policies]

    # Map existing keyword keys (lowercase) to ensure custom rule preservation
    existing_kw_keys_lower = {k.lower(): k for k in existing_keywords.keys()}

    for pack_id in pack_ids:
        pack = get_starter_template_by_id(pack_id)
        if not pack:
            continue

        # Merge keyword rules safely (do NOT overwrite existing keywords)
        for kw, target_path in pack.keyword_rules.items():
            kw_lower = kw.lower()
            if kw_lower not in existing_kw_keys_lower and kw not in new_keywords:
                new_keywords[kw] = target_path

        # Merge policies safely (do NOT add duplicate type + expression)
        for policy in pack.policies:
            p_type = str(policy.get("type", "")).lower()
            p_expr = str(policy.get("expression", "")).lower()

            is_duplicate = False
            for existing_p in new_policies:
                ex_type = str(existing_p.get("type", "")).lower()
                ex_expr = str(existing_p.get("expression", "")).lower()
                if ex_type == p_type and ex_expr == p_expr:
                    is_duplicate = True
                    break

            if not is_duplicate:
                new_policies.append(dict(policy))

    # Apply to settings
    setattr(settings, "KEYWORD_RULES", new_keywords)
    setattr(settings, "POLICIES", new_policies)

    if hasattr(settings, "_save"):
        try:
            settings._save()
        except Exception:
            pass

    return new_keywords, new_policies
