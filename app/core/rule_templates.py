"""Starter rule template gallery and rule preset integration module.

Provides built-in starter rule template packs covering common document workflows
and safe merging utilities that preserve custom user rules.
"""

import configparser
import json
import logging
import os
import plistlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class StarterTemplatePack:
    """Data container for a starter rule template pack."""

    id: str
    title: str
    description: str
    keyword_rules: dict[str, str] = field(default_factory=dict)
    policies: list[dict[str, Any]] = field(default_factory=list)
    icon: str = "📂"


BUILTIN_STARTER_TEMPLATES: list[StarterTemplatePack] = [
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


def get_starter_templates() -> list[StarterTemplatePack]:
    """Return list of built-in starter rule template packs."""
    return list(BUILTIN_STARTER_TEMPLATES)


def get_starter_template_by_id(pack_id: str) -> Optional[StarterTemplatePack]:
    """Find a starter template pack by unique ID."""
    for pack in BUILTIN_STARTER_TEMPLATES:
        if pack.id == pack_id:
            return pack
    return None


def apply_starter_templates(
    settings: Any,
    pack_ids: list[str],
    *,
    folder: str | Path | None = None,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Merge selected starter template packs into settings without overwriting existing custom rules.

    Args:
        settings: Application settings object (`AppSettings` or `ScopedSettingsView`).
        pack_ids: List of template pack IDs to apply.
        folder: Optional target workspace folder path to scope starter templates to.

    Returns
    -------
        Tuple of updated (KEYWORD_RULES dict, POLICIES list).
    """
    from app.config import resolve_scoped_settings

    target_settings = (
        resolve_scoped_settings(settings, folder, fallback_to_global=True)
        if folder is not None
        else settings
    )
    existing_keywords = dict(getattr(target_settings, "KEYWORD_RULES", {}) or {})
    existing_policies = list(getattr(target_settings, "POLICIES", []) or [])

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
    setattr(target_settings, "KEYWORD_RULES", new_keywords)
    setattr(target_settings, "POLICIES", new_policies)

    if hasattr(target_settings, "_save"):
        target_settings._save()

    return new_keywords, new_policies


def export_active_rules(
    settings: Any,
    target_format: str,
    destination_path: str | Path,
    *,
    folder: str | Path | None = None,
) -> Path:
    """Serialize rules from active settings into the selected target format and save to disk.

    Args:
        settings: Application settings object (`AppSettings`) containing active rules.
        target_format: Selected export format ('sortify_json', 'hazel', or 'dropit_ini').
        destination_path: File path on disk where rules should be exported.

    Returns
    -------
        Path object pointing to the written exported rules file.

    Raises
    ------
        ValueError: If destination path is invalid, contains traversal segments, or format is unsupported.
        OSError: If destination path or parent directory cannot be written to.
    """
    if destination_path is None or not str(destination_path).strip():
        raise ValueError("Destination file path cannot be empty.")

    path_str = str(destination_path).strip()
    if "\0" in path_str:
        raise ValueError("Invalid destination path: contains null characters.")

    raw_path = Path(path_str)
    if ".." in raw_path.parts:
        raise ValueError(
            "Directory traversal segments ('..') are not allowed in export destination path."
        )

    dest_path = raw_path.expanduser().resolve()
    if dest_path.is_dir():
        raise ValueError(
            f"Destination path '{dest_path}' is an existing directory, not a file."
        )

    # Guard and create parent directory
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(
            f"Cannot create destination directory '{dest_path.parent}': {exc}"
        ) from exc

    if not os.access(dest_path.parent, os.W_OK):
        raise OSError(f"Destination directory '{dest_path.parent}' is not writable.")
    if dest_path.exists() and not os.access(dest_path, os.W_OK):
        raise OSError(f"Destination file '{dest_path}' is not writable.")

    import copy

    from app.config import resolve_scoped_settings

    fmt_clean = str(target_format).strip().lower().replace(" ", "_").replace(".", "")

    if "sortify" in fmt_clean or fmt_clean in ("json", "sortify_json"):
        if folder is not None and str(folder).strip():
            eff_source = resolve_scoped_settings(
                settings, folder, fallback_to_global=False
            )
            exported_scopes: dict[str, dict[str, Any]] = {}
        else:
            eff_source = getattr(settings, "root_settings", settings)
            raw_scopes = getattr(eff_source, "FOLDER_RULE_SCOPES", {}) or {}
            exported_scopes = (
                copy.deepcopy(dict(raw_scopes)) if isinstance(raw_scopes, dict) else {}
            )

        keyword_rules = dict(getattr(eff_source, "KEYWORD_RULES", {}) or {})
        disabled_keyword_rules = dict(
            getattr(eff_source, "DISABLED_KEYWORD_RULES", {}) or {}
        )
        learned_rules = dict(getattr(eff_source, "LEARNED_RULES", {}) or {})
        disabled_learned_rules = dict(
            getattr(eff_source, "DISABLED_LEARNED_RULES", {}) or {}
        )
        raw_ext_cats = getattr(eff_source, "EXTENSION_CATEGORIES", {}) or {}
        extension_categories = (
            copy.deepcopy(dict(raw_ext_cats)) if isinstance(raw_ext_cats, dict) else {}
        )
        policies = [dict(p) for p in (getattr(eff_source, "POLICIES", []) or [])]

        payload = {
            "format": "sortify",
            "version": 2,
            "KEYWORD_RULES": keyword_rules,
            "DISABLED_KEYWORD_RULES": disabled_keyword_rules,
            "LEARNED_RULES": learned_rules,
            "DISABLED_LEARNED_RULES": disabled_learned_rules,
            "EXTENSION_CATEGORIES": extension_categories,
            "POLICIES": policies,
            "FOLDER_RULE_SCOPES": exported_scopes,
        }
        dest_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return dest_path

    # Non-destructive: extract rules for external formats (scoped when folder is given)
    ext_source = (
        resolve_scoped_settings(settings, folder, fallback_to_global=True)
        if folder is not None and str(folder).strip()
        else settings
    )
    keyword_rules = dict(getattr(ext_source, "KEYWORD_RULES", {}) or {})
    policies = [dict(p) for p in (getattr(ext_source, "POLICIES", []) or [])]

    if "hazel" in fmt_clean or fmt_clean in ("hazelrules",):
        rules_list: list[dict[str, Any]] = []
        for kw, target_dir in keyword_rules.items():
            kw_clean = str(kw).strip()
            target_clean = str(target_dir).strip()
            rules_list.append(
                {
                    "NSRuleName": f"Rule: {kw_clean}",
                    "NSRuleIsEnabled": True,
                    "NSRuleConditions": [
                        {
                            "target": "Name",
                            "operator": "contains",
                            "value": kw_clean,
                            "NSRuleConditionTarget": "Name",
                            "NSRuleConditionOperator": "contains",
                            "NSRuleConditionValue": kw_clean,
                        }
                    ],
                    "NSRuleActions": [
                        {
                            "NSRuleActionType": "NSRuleActionMove",
                            "DestinationPath": target_clean,
                        }
                    ],
                }
            )

        for idx, pol in enumerate(policies):
            p_type = str(pol.get("type", "keyword")).strip().lower()
            p_action = str(pol.get("action", "") or "").strip().lower()
            p_expr = str(pol.get("expression", "")).strip()
            p_target = str(pol.get("target_path", "")).strip()
            p_rename = str(pol.get("rename_pattern", "") or "").strip()
            if not p_expr and p_type == "duplicate":
                p_expr = "*"
            if not p_expr:
                continue

            is_trash_policy = p_action == "trash" or (
                p_type == "duplicate" and not p_target
            )

            cond_val = p_expr
            if p_type == "pattern":
                cond_target = (
                    "Extension" if ("." in p_expr and "*" not in p_expr) else "Name"
                )
                cond_op = "matches"
            elif p_type == "duplicate":
                cond_target = "Duplicate"
                cond_op = "is"
                cond_val = "true" if p_expr in ("*", "") else p_expr
            elif p_type in (
                "size",
                "age",
                "modified_date",
                "created_date",
                "exif_date",
            ):
                target_map = {
                    "size": "Size",
                    "age": "Age",
                    "modified_date": "DateModified",
                    "created_date": "DateCreated",
                    "exif_date": "EXIFDate",
                }
                cond_target = target_map[p_type]
                expr_lower = p_expr.lower()
                if expr_lower.startswith("older than "):
                    cond_op = "older than"
                    cond_val = p_expr[len("older than ") :].strip()
                elif expr_lower.startswith("in the last "):
                    cond_op = "in the last"
                    cond_val = p_expr[len("in the last ") :].strip()
                elif p_expr.startswith((">=", "<=", "==")):
                    cond_op = "=" if p_expr[:2] == "==" else p_expr[:2]
                    cond_val = p_expr[2:].strip()
                elif p_expr.startswith((">", "<", "=")):
                    cond_op = p_expr[:1]
                    cond_val = p_expr[1:].strip()
                else:
                    cond_op = "is"
                    cond_val = p_expr
            else:
                cond_target = "Name"
                cond_op = "contains"

            if is_trash_policy:
                hazel_act_type = "NSRuleActionTrash"
            elif p_action == "unarchive" or p_type == "unarchive":
                hazel_act_type = "NSRuleActionUnarchive"
            else:
                hazel_act_type = "NSRuleActionMove"

            rule_actions: list[dict[str, Any]] = [
                {
                    "NSRuleActionType": hazel_act_type,
                    "DestinationPath": "" if is_trash_policy else p_target,
                }
            ]
            if p_rename and not is_trash_policy:
                rule_actions.append(
                    {
                        "NSRuleActionType": "NSRuleActionRename",
                        "RenamePattern": p_rename,
                    }
                )

            rules_list.append(
                {
                    "NSRuleName": f"Policy {idx + 1}: {p_expr}",
                    "NSRuleIsEnabled": bool(pol.get("enabled", True)),
                    "NSRuleConditions": [
                        {
                            "target": cond_target,
                            "operator": cond_op,
                            "value": cond_val,
                            "NSRuleConditionTarget": cond_target,
                            "NSRuleConditionOperator": cond_op,
                            "NSRuleConditionValue": cond_val,
                        }
                    ],
                    "NSRuleActions": rule_actions,
                }
            )

        hazel_data = {"Rules": rules_list}
        with open(dest_path, "wb") as f:
            plistlib.dump(hazel_data, f, sort_keys=True)
        return dest_path

    elif "dropit" in fmt_clean or fmt_clean in ("ini", "dropit_ini"):
        config = configparser.ConfigParser(interpolation=None)
        config.optionxform = lambda optionstr: optionstr
        config["DropIt"] = {
            "Profile": "Sortify Exported Rules",
            "Version": "8.5",
        }
        assoc_idx = 1
        for kw, target_dir in keyword_rules.items():
            sec_name = f"Association_{assoc_idx}"
            config[sec_name] = {
                "Name": str(kw).strip(),
                "Rules": str(kw).strip(),
                "Action": "Move",
                "Destination": str(target_dir).strip(),
            }
            assoc_idx += 1

        for pol in policies:
            if pol.get("enabled", True) is False:
                continue
            p_type = str(pol.get("type", "keyword")).strip().lower()
            p_action = str(pol.get("action", "") or "").strip().lower()
            p_expr = str(pol.get("expression", "")).strip()
            p_target = str(pol.get("target_path", "")).strip()
            if not p_expr and p_type == "duplicate":
                p_expr = "*"
            if not p_expr:
                continue
            is_trash_policy = p_action == "trash" or (
                p_type == "duplicate" and not p_target
            )
            if is_trash_policy:
                dropit_act = "Delete"
                dropit_dest = ""
            elif p_action == "unarchive" or p_type == "unarchive":
                dropit_act = "Extract"
                dropit_dest = p_target
            else:
                dropit_act = "Move"
                dropit_dest = p_target
            sec_name = f"Association_{assoc_idx}"
            config[sec_name] = {
                "Name": p_expr,
                "Rules": p_expr,
                "Action": dropit_act,
                "Destination": dropit_dest,
            }
            assoc_idx += 1

        with open(dest_path, "w", encoding="utf-8") as f:
            config.write(f)
        return dest_path

    else:
        raise ValueError(
            f"Unsupported export format '{target_format}'. Supported formats: 'sortify_json', 'hazel', 'dropit_ini'."
        )
