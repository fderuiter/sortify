"""Starter rule template gallery and rule preset integration module.

Provides built-in starter rule template packs covering common document workflows
and safe merging utilities that preserve custom user rules.
"""

import configparser
import copy
import json
import logging
import os
import plistlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.core.rule_importers import _find_matching_policy_index

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
    settings: Any, pack_ids: list[str]
) -> tuple[dict[str, str], list[dict[str, Any]]]:
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
            if (
                _find_matching_policy_index(new_policies, policy, match_signature=False)
                is None
            ):
                new_policies.append(dict(policy))

    # Apply to settings
    setattr(settings, "KEYWORD_RULES", new_keywords)
    setattr(settings, "POLICIES", new_policies)

    if hasattr(settings, "_save"):
        settings._save()

    return new_keywords, new_policies


@dataclass(frozen=True)
class _ExportPolicyEntry:
    """Normalized policy fields prepared for Hazel and DropIt export."""

    index: int
    policy_type: str
    action: str
    expression: str
    target_path: str
    rename_pattern: str
    enabled: bool
    is_unarchive: bool
    match_mode: str
    has_explicit_match_mode: bool
    negate: bool
    regex: bool
    case_sensitive: bool
    conditions: list[dict[str, Any]] | None


def _iter_export_keywords(keyword_rules: dict[str, str]) -> Iterator[tuple[str, str]]:
    """Yield stripped ``(keyword, target_dir)`` pairs for external export."""
    for kw, target_dir in keyword_rules.items():
        yield str(kw).strip(), str(target_dir).strip()


def _iter_export_policies(
    policies: list[dict[str, Any]],
) -> Iterator[_ExportPolicyEntry]:
    """Yield normalized exportable policy entries with non-empty expressions or conditions."""
    for idx, pol in enumerate(policies):
        p_type = str(pol.get("type", "keyword")).strip().lower()
        p_action = str(pol.get("action", "") or "").strip().lower()
        p_expr = str(pol.get("expression", "")).strip()
        raw_conds = pol.get("conditions")
        conds = (
            raw_conds if (isinstance(raw_conds, list) and len(raw_conds) > 0) else None
        )
        if not p_expr and conds is None:
            continue
        yield _ExportPolicyEntry(
            index=idx,
            policy_type=p_type,
            action=p_action,
            expression=p_expr,
            target_path=str(pol.get("target_path", "")).strip(),
            rename_pattern=str(pol.get("rename_pattern", "") or "").strip(),
            enabled=bool(pol.get("enabled", True)),
            is_unarchive=(p_action == "unarchive" or p_type == "unarchive"),
            match_mode=str(pol.get("match_mode", "all") or "all").strip().lower(),
            has_explicit_match_mode=("match_mode" in pol),
            negate=bool(pol.get("negate", False)),
            regex=bool(pol.get("regex", False)),
            case_sensitive=bool(pol.get("case_sensitive", False)),
            conditions=conds,
        )


def _build_hazel_policy_condition(
    p_type: str, p_expr: str, *, regex: bool = False
) -> tuple[str, str, str]:
    """Map a Sortify policy type and expression to Hazel ``(target, operator, value)``."""
    if regex:
        return "Name", "matches regex", p_expr
    if p_type == "pattern":
        cond_target = "Extension" if ("." in p_expr and "*" not in p_expr) else "Name"
        return cond_target, "matches", p_expr
    if p_type == "duplicate":
        return "Duplicate", "is", p_expr
    if p_type in ("size", "age", "modified_date", "created_date", "exif_date"):
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
            return cond_target, "older than", p_expr[len("older than ") :].strip()
        if expr_lower.startswith("in the last "):
            return cond_target, "in the last", p_expr[len("in the last ") :].strip()
        if p_expr.startswith((">=", "<=", "==")):
            cond_op = "=" if p_expr[:2] == "==" else p_expr[:2]
            return cond_target, cond_op, p_expr[2:].strip()
        if p_expr.startswith((">", "<", "=")):
            return cond_target, p_expr[:1], p_expr[1:].strip()
        return cond_target, "is", p_expr
    return "Name", "contains", p_expr


def _format_dropit_clause_mask(field_name: str, operator: str, value: str) -> str:
    """Format a leaf condition clause into a DropIt mask string."""
    if field_name == "extension":
        return f"*.{value.lstrip('.')}"
    if operator == "contains" and "*" not in value:
        return f"*{value}*"
    return value


def _format_dropit_compound_rules(entry: _ExportPolicyEntry) -> tuple[str, bool]:
    """Translate compound policy conditions into DropIt ``(rules_val, needs_structured_meta)``."""
    raw_conds = entry.conditions
    if not raw_conds:
        return entry.expression, False

    pos_masks: list[str] = []
    exc_masks: list[str] = []
    can_express_as_dropit_slash = entry.match_mode == "all" and not entry.negate

    for cond in raw_conds:
        if not isinstance(cond, dict):
            continue
        sub_conds = cond.get("conditions")
        if isinstance(sub_conds, list):
            if (
                cond.get("match_mode") == "any"
                and not cond.get("negate")
                and not pos_masks
            ):
                for sc in sub_conds:
                    if (
                        isinstance(sc, dict)
                        and sc.get("field") in ("name", "extension")
                        and sc.get("operator") in ("glob", "equals", "contains")
                        and not sc.get("negate")
                        and not sc.get("case_sensitive")
                    ):
                        pos_masks.append(
                            _format_dropit_clause_mask(
                                str(sc.get("field", "name")),
                                str(sc.get("operator", "contains")),
                                str(sc.get("value", "")),
                            )
                        )
                    else:
                        can_express_as_dropit_slash = False
            else:
                can_express_as_dropit_slash = False
            continue

        c_fld = str(cond.get("field", "name")).lower()
        c_op = str(cond.get("operator", "contains")).lower()
        c_val = str(cond.get("value", ""))
        if (
            c_fld in ("name", "extension")
            and c_op in ("glob", "equals", "contains")
            and not cond.get("case_sensitive")
            and "/" not in c_val
            and ";" not in c_val
        ):
            mask_repr = _format_dropit_clause_mask(c_fld, c_op, c_val)
            if c_op == "equals" and c_fld == "name":
                can_express_as_dropit_slash = False
            if cond.get("negate"):
                exc_masks.append(mask_repr)
            else:
                pos_masks.append(mask_repr)
        else:
            can_express_as_dropit_slash = False

    if can_express_as_dropit_slash and pos_masks and exc_masks:
        if len(pos_masks) == 1:
            return f"{pos_masks[0]}/{';'.join(exc_masks)}", False
        return f"{';'.join(pos_masks)}/{';'.join(exc_masks)}", False

    if pos_masks and exc_masks:
        return f"{';'.join(pos_masks)}/{';'.join(exc_masks)}", True
    if pos_masks:
        return ";".join(pos_masks), True
    return entry.expression, True


def export_active_rules(
    settings: Any,
    target_format: str,
    destination_path: str | Path,
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

    # Non-destructive: extract rules without mutating settings
    keyword_rules = dict(getattr(settings, "KEYWORD_RULES", {}) or {})
    policies = [
        copy.deepcopy(dict(p)) for p in (getattr(settings, "POLICIES", []) or [])
    ]

    fmt_clean = str(target_format).strip().lower().replace(" ", "_").replace(".", "")

    if "sortify" in fmt_clean or fmt_clean in ("json", "sortify_json"):
        payload = {
            "format": "sortify",
            "version": 1,
            "KEYWORD_RULES": keyword_rules,
            "POLICIES": policies,
        }
        dest_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return dest_path

    elif "hazel" in fmt_clean or fmt_clean in ("hazelrules",):
        hazel_field_map = {
            "name": "Name",
            "extension": "Extension",
            "content": "Contents",
            "path": "Full Path",
            "any_text": "Any",
        }
        hazel_op_map = {
            "contains": "contains",
            "equals": "is",
            "starts_with": "starts with",
            "ends_with": "ends with",
            "glob": "matches",
            "regex": "matches regex",
        }

        def _serialize_hazel_node(
            node: dict[str, Any], default_case: bool = False
        ) -> dict[str, Any]:
            if isinstance(node.get("conditions"), list):
                grp_mode = str(node.get("match_mode", "all") or "all").lower()
                grp_dict: dict[str, Any] = {
                    "NSRuleConditionMode": grp_mode,
                    "ConditionMode": grp_mode,
                    "NSRuleConditions": [
                        _serialize_hazel_node(c, default_case)
                        for c in node["conditions"]
                        if isinstance(c, dict)
                    ],
                }
                if node.get("negate"):
                    grp_dict["negated"] = True
                    grp_dict["NSRuleConditionNegated"] = True
                return grp_dict

            fld = str(node.get("field", "name")).lower()
            op = str(node.get("operator", "contains")).lower()
            val = str(node.get("value", ""))
            hz_target = hazel_field_map.get(fld, "Name")
            hz_op = hazel_op_map.get(op, "contains")
            cond_entry: dict[str, Any] = {
                "target": hz_target,
                "operator": hz_op,
                "value": val,
                "NSRuleConditionTarget": hz_target,
                "NSRuleConditionOperator": hz_op,
                "NSRuleConditionValue": val,
            }
            if node.get("negate"):
                cond_entry["negated"] = True
                cond_entry["NSRuleConditionNegated"] = True
            if op == "regex":
                cond_entry["regex"] = True
                cond_entry["NSRuleConditionRegex"] = True
            if node.get("case_sensitive") or (
                default_case and "case_sensitive" not in node
            ):
                cond_entry["caseSensitive"] = True
                cond_entry["NSRuleConditionCaseSensitive"] = True
            return cond_entry

        rules_list: list[dict[str, Any]] = []
        for kw_clean, target_clean in _iter_export_keywords(keyword_rules):
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

        for entry in _iter_export_policies(policies):
            if entry.action == "trash":
                hazel_act_type = "NSRuleActionTrash"
            elif entry.is_unarchive:
                hazel_act_type = "NSRuleActionUnarchive"
            else:
                hazel_act_type = "NSRuleActionMove"

            rule_actions: list[dict[str, Any]] = [
                {
                    "NSRuleActionType": hazel_act_type,
                    "DestinationPath": entry.target_path,
                }
            ]
            if entry.rename_pattern and entry.action != "trash":
                rule_actions.append(
                    {
                        "NSRuleActionType": "NSRuleActionRename",
                        "RenamePattern": entry.rename_pattern,
                    }
                )

            if entry.conditions is not None:
                hz_conds = [
                    _serialize_hazel_node(c, default_case=entry.case_sensitive)
                    for c in entry.conditions
                    if isinstance(c, dict)
                ]
                rule_entry: dict[str, Any] = {
                    "NSRuleName": f"Policy {entry.index + 1}: {entry.expression}",
                    "NSRuleIsEnabled": entry.enabled,
                    "NSRuleConditionMode": entry.match_mode,
                    "NSRuleConditions": hz_conds,
                    "NSRuleActions": rule_actions,
                }
                if entry.negate:
                    rule_entry["NSRuleNegated"] = True
                rules_list.append(rule_entry)
                continue

            cond_target, cond_op, cond_val = _build_hazel_policy_condition(
                entry.policy_type, entry.expression, regex=entry.regex
            )
            single_cond: dict[str, Any] = {
                "target": cond_target,
                "operator": cond_op,
                "value": cond_val,
                "NSRuleConditionTarget": cond_target,
                "NSRuleConditionOperator": cond_op,
                "NSRuleConditionValue": cond_val,
            }
            if entry.regex:
                single_cond["regex"] = True
                single_cond["NSRuleConditionRegex"] = True
            if entry.case_sensitive:
                single_cond["caseSensitive"] = True
                single_cond["NSRuleConditionCaseSensitive"] = True

            rule_entry = {
                "NSRuleName": f"Policy {entry.index + 1}: {entry.expression}",
                "NSRuleIsEnabled": entry.enabled,
                "NSRuleConditions": [single_cond],
                "NSRuleActions": rule_actions,
            }
            if entry.match_mode in ("all", "any", "none") and (
                entry.has_explicit_match_mode or entry.negate
            ):
                rule_entry["NSRuleConditionMode"] = (
                    "none"
                    if (entry.negate and entry.match_mode != "none")
                    else entry.match_mode
                )
            rules_list.append(rule_entry)

        hazel_data = {"Rules": rules_list}
        with open(dest_path, "wb") as f:
            plistlib.dump(hazel_data, f, sort_keys=True)
        return dest_path

    elif "dropit" in fmt_clean or fmt_clean in ("ini", "dropit_ini"):
        config = configparser.ConfigParser(interpolation=None)
        config.optionxform = lambda optionstr: optionstr  # type: ignore[method-assign]
        config["DropIt"] = {
            "Profile": "Sortify Exported Rules",
            "Version": "8.5",
        }
        assoc_idx = 1
        for kw_clean, target_clean in _iter_export_keywords(keyword_rules):
            sec_name = f"Association_{assoc_idx}"
            config[sec_name] = {
                "Name": kw_clean,
                "Rules": kw_clean,
                "Action": "Move",
                "Destination": target_clean,
            }
            assoc_idx += 1

        for entry in _iter_export_policies(policies):
            if entry.action == "trash":
                dropit_act = "Delete"
            elif entry.is_unarchive:
                dropit_act = "Extract"
            else:
                dropit_act = "Move"

            rules_val, needs_structured_meta = _format_dropit_compound_rules(entry)
            has_conds = entry.conditions is not None

            sec_name = f"Association_{assoc_idx}"
            sec_dict: dict[str, str] = {
                "Name": entry.expression,
                "Rules": rules_val,
                "Action": dropit_act,
                "Destination": entry.target_path,
            }
            if entry.regex:
                sec_dict["Regex"] = "true"
            if entry.case_sensitive:
                sec_dict["CaseSensitive"] = "true"
            if entry.negate or (not has_conds and entry.match_mode == "none"):
                sec_dict["Negate"] = "true"
            if entry.conditions is not None and (
                needs_structured_meta
                or entry.match_mode != "all"
                or entry.negate
                or entry.case_sensitive
            ):
                sec_dict["MatchMode"] = entry.match_mode
                sec_dict["SortifyConditions"] = json.dumps(
                    entry.conditions, ensure_ascii=False
                )
            config[sec_name] = sec_dict
            assoc_idx += 1

        with open(dest_path, "w", encoding="utf-8") as f:
            config.write(f)
        return dest_path

    else:
        raise ValueError(
            f"Unsupported export format '{target_format}'. Supported formats: 'sortify_json', 'hazel', 'dropit_ini'."
        )
