"""CLI CRUD subcommand handlers (list, add, remove, enable, disable) for Sortify rules."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections.abc import Callable
from typing import Any

from app.cli.error_emitter import emit_cli_error
from app.cli.settings_args import build_global_subparser_parent
from app.config import AppSettings
from app.core.metadata import matches_learned_rule_pattern

_RULES_CRUD_SETTINGS_HELP = "Optional path to a specific Sortify settings.json file"

_VALID_RULE_KINDS = ("keyword", "learned", "extension", "policy")
_VALID_POLICY_TYPES = (
    "keyword",
    "pattern",
    "override",
    "action",
    "compliance",
    "unarchive",
    "duplicate",
    "age",
    "modified_date",
    "created_date",
    "exif_date",
    "size",
    "glob",
    "regex",
    "extension",
)
_POLICY_TYPE_ALIASES = {
    "glob": "pattern",
    "regex": "pattern",
    "extension": "pattern",
    "duplicate": "duplicate",
}
_VALID_POLICY_ACTIONS = (
    "move",
    "copy",
    "rename",
    "quarantine",
    "archive",
    "unarchive",
    "retain",
    "redact",
    "trash",
    "duplicate",
)


def register_crud_subparsers(
    rules_subparsers: argparse._SubParsersAction,
    add_rules_common: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Register ``list``, ``add``, ``remove``, ``enable``, and ``disable`` subparsers."""
    p_list = rules_subparsers.add_parser(
        "list",
        parents=[
            build_global_subparser_parent(settings_help=_RULES_CRUD_SETTINGS_HELP)
        ],
        help="List active and disabled rules in global or folder-scoped settings",
    )
    add_rules_common(p_list)
    p_list.add_argument(
        "--kind",
        type=str,
        choices=["all", *_VALID_RULE_KINDS],
        default="all",
        help="Filter rules by kind (all, keyword, learned, extension, policy)",
    )
    p_list.add_argument(
        "--status",
        type=str,
        choices=["all", "active", "disabled"],
        default="all",
        help="Filter rules by state (all, active, disabled)",
    )

    p_add = rules_subparsers.add_parser(
        "add",
        parents=[
            build_global_subparser_parent(settings_help=_RULES_CRUD_SETTINGS_HELP)
        ],
        help="Add or update a keyword, learned, extension, or policy rule",
    )
    add_rules_common(p_add)
    p_add.add_argument(
        "pattern_pos",
        nargs="?",
        default=None,
        metavar="PATTERN",
        help="Keyword, learned stem, comma-separated extensions, or policy expression",
    )
    p_add.add_argument(
        "target_pos",
        nargs="?",
        default=None,
        metavar="TARGET",
        help="Relative destination folder path (e.g. Finances/Invoices)",
    )
    p_add.add_argument(
        "--pattern",
        dest="pattern_opt",
        type=str,
        default=None,
        help="Rule pattern or expression",
    )
    p_add.add_argument(
        "--target",
        dest="target_opt",
        type=str,
        default=None,
        help="Relative destination folder path",
    )
    p_add.add_argument(
        "--kind",
        type=str,
        choices=list(_VALID_RULE_KINDS),
        default="keyword",
        help="Rule table kind (default: keyword)",
    )
    p_add.add_argument(
        "--type",
        dest="policy_type",
        type=str,
        choices=list(_VALID_POLICY_TYPES),
        default="keyword",
        help="Condition type when --kind=policy (default: keyword)",
    )
    p_add.add_argument(
        "--action",
        dest="policy_action",
        type=str,
        choices=list(_VALID_POLICY_ACTIONS),
        default=None,
        help="Action when --kind=policy (e.g. move, copy, trash, quarantine)",
    )
    p_add.add_argument(
        "--priority",
        type=int,
        default=75,
        help="Evaluation priority (0-100) when --kind=policy (default: 75)",
    )
    p_add.add_argument(
        "--no-halting",
        action="store_false",
        dest="halting",
        default=True,
        help="Continue evaluating lower-priority policies after match",
    )
    p_add.add_argument(
        "--rename-pattern",
        type=str,
        default=None,
        help="Optional rename template when --kind=policy",
    )
    p_add.add_argument(
        "--disabled",
        action="store_true",
        default=False,
        help="Create the rule in disabled state",
    )

    p_remove = rules_subparsers.add_parser(
        "remove",
        parents=[
            build_global_subparser_parent(settings_help=_RULES_CRUD_SETTINGS_HELP)
        ],
        help="Remove a rule by pattern or expression",
    )
    add_rules_common(p_remove)
    p_remove.add_argument(
        "pattern_pos",
        nargs="?",
        default=None,
        metavar="PATTERN",
        help="Rule pattern, extension, category, or policy expression to remove",
    )
    p_remove.add_argument(
        "--pattern",
        dest="pattern_opt",
        type=str,
        default=None,
        help="Rule pattern, extension, category, or policy expression to remove",
    )
    p_remove.add_argument(
        "--kind",
        type=str,
        choices=["all", *_VALID_RULE_KINDS],
        default="all",
        help="Restrict removal to a specific rule kind (default: all)",
    )

    p_enable = rules_subparsers.add_parser(
        "enable",
        parents=[
            build_global_subparser_parent(settings_help=_RULES_CRUD_SETTINGS_HELP)
        ],
        help="Enable a disabled keyword, learned, or policy rule",
    )
    add_rules_common(p_enable)
    p_enable.add_argument(
        "pattern_pos",
        nargs="?",
        default=None,
        metavar="PATTERN",
        help="Rule pattern or policy expression to enable",
    )
    p_enable.add_argument(
        "--pattern",
        dest="pattern_opt",
        type=str,
        default=None,
        help="Rule pattern or policy expression to enable",
    )
    p_enable.add_argument(
        "--kind",
        type=str,
        choices=["all", "keyword", "learned", "policy"],
        default="all",
        help="Restrict enable operation to a specific rule kind (default: all)",
    )

    p_disable = rules_subparsers.add_parser(
        "disable",
        parents=[
            build_global_subparser_parent(settings_help=_RULES_CRUD_SETTINGS_HELP)
        ],
        help="Disable an active keyword, learned, or policy rule",
    )
    add_rules_common(p_disable)
    p_disable.add_argument(
        "pattern_pos",
        nargs="?",
        default=None,
        metavar="PATTERN",
        help="Rule pattern or policy expression to disable",
    )
    p_disable.add_argument(
        "--pattern",
        dest="pattern_opt",
        type=str,
        default=None,
        help="Rule pattern or policy expression to disable",
    )
    p_disable.add_argument(
        "--kind",
        type=str,
        choices=["all", "keyword", "learned", "policy"],
        default="all",
        help="Restrict disable operation to a specific rule kind (default: all)",
    )


def _collect_rule_rows(
    view: Any,
    *,
    scope_label: str,
    kind_filter: str = "all",
    status_filter: str = "all",
) -> list[dict[str, Any]]:
    """Collect normalized rule rows across keyword, learned, extension, and policy tables."""
    rows: list[dict[str, Any]] = []

    if kind_filter in ("all", "keyword"):
        if status_filter in ("all", "active"):
            for k, v in (getattr(view, "KEYWORD_RULES", {}) or {}).items():
                rows.append(
                    {
                        "kind": "keyword",
                        "pattern": str(k),
                        "target": str(v),
                        "enabled": True,
                        "status": "active",
                        "priority": 80,
                        "scope": scope_label,
                    }
                )
        if status_filter in ("all", "disabled"):
            for k, v in (getattr(view, "DISABLED_KEYWORD_RULES", {}) or {}).items():
                rows.append(
                    {
                        "kind": "keyword",
                        "pattern": str(k),
                        "target": str(v),
                        "enabled": False,
                        "status": "disabled",
                        "priority": 80,
                        "scope": scope_label,
                    }
                )

    if kind_filter in ("all", "learned"):
        if status_filter in ("all", "active"):
            for k, v in (getattr(view, "LEARNED_RULES", {}) or {}).items():
                rows.append(
                    {
                        "kind": "learned",
                        "pattern": str(k),
                        "target": str(v),
                        "enabled": True,
                        "status": "active",
                        "priority": 90,
                        "scope": scope_label,
                    }
                )
        if status_filter in ("all", "disabled"):
            for k, v in (getattr(view, "DISABLED_LEARNED_RULES", {}) or {}).items():
                rows.append(
                    {
                        "kind": "learned",
                        "pattern": str(k),
                        "target": str(v),
                        "enabled": False,
                        "status": "disabled",
                        "priority": 90,
                        "scope": scope_label,
                    }
                )

    if kind_filter in ("all", "extension") and status_filter in ("all", "active"):
        ext_map = getattr(view, "EXTENSION_CATEGORIES", {}) or {}
        if isinstance(ext_map, dict):
            for cat, raw_exts in ext_map.items():
                if isinstance(raw_exts, dict):
                    for subcat, sub_exts in raw_exts.items():
                        ext_list = [str(e) for e in (sub_exts or [])]
                        rows.append(
                            {
                                "kind": "extension",
                                "pattern": ", ".join(ext_list),
                                "extensions": ext_list,
                                "target": f"{cat}/{subcat}",
                                "enabled": True,
                                "status": "active",
                                "priority": 50,
                                "scope": scope_label,
                            }
                        )
                elif isinstance(raw_exts, list):
                    ext_list = [str(e) for e in raw_exts]
                    rows.append(
                        {
                            "kind": "extension",
                            "pattern": ", ".join(ext_list),
                            "extensions": ext_list,
                            "target": str(cat),
                            "enabled": True,
                            "status": "active",
                            "priority": 50,
                            "scope": scope_label,
                        }
                    )

    if kind_filter in ("all", "policy"):
        for pol in getattr(view, "POLICIES", []) or []:
            if not isinstance(pol, dict):
                continue
            enabled = pol.get("enabled") is not False
            status_str = "active" if enabled else "disabled"
            if status_filter not in ("all", status_str):
                continue
            row: dict[str, Any] = {
                "kind": "policy",
                "policy_type": str(pol.get("type", "keyword")),
                "pattern": str(pol.get("expression", "")),
                "target": str(pol.get("target_path", "")),
                "action": str(pol.get("action") or "move"),
                "priority": int(pol.get("priority", 75)),
                "halting": bool(pol.get("halting", True)),
                "enabled": enabled,
                "status": status_str,
                "scope": scope_label,
            }
            if "match_mode" in pol:
                row["match_mode"] = str(pol.get("match_mode", "all"))
            if isinstance(pol.get("conditions"), list) and pol.get("conditions"):
                row["conditions"] = list(pol["conditions"])
            rows.append(row)

    return rows


def _extract_pattern_arg(
    args: argparse.Namespace, *, subcmd: str, json_mode: bool
) -> str:
    """Resolve ``PATTERN`` from ``--pattern`` or positional argument."""
    raw_pat = getattr(args, "pattern_opt", None)
    if raw_pat is None:
        raw_pat = getattr(args, "pattern_pos", None)
    if raw_pat is None or not str(raw_pat).strip():
        emit_cli_error(
            f"Error: Missing rule pattern for 'rules {subcmd}'.",
            json_mode=json_mode,
            exit_code=2,
        )
    return str(raw_pat).strip()


def _find_dict_key_case_insensitive(mapping: dict[str, str], query: str) -> str | None:
    """Find matching key in ``mapping`` by exact match first, then case-insensitive match."""
    if query in mapping:
        return query
    q_lower = query.lower()
    for k in mapping:
        if str(k).lower() == q_lower:
            return k
    return None


def _find_learned_keys(mapping: dict[str, str], query: str) -> list[str]:
    """Find matching keys in ``LEARNED_RULES`` / ``DISABLED_LEARNED_RULES``."""
    if query in mapping:
        return [query]
    return [k for k in mapping if matches_learned_rule_pattern(str(k), query)]


def handle_rules_crud_subcommand(
    rules_cmd: str,
    args: argparse.Namespace,
    *,
    root_settings: AppSettings,
    target_view: Any,
    scope_label: str,
    is_json: bool,
    quiet: bool,
) -> bool:
    """Execute ``list``, ``add``, ``remove``, ``enable``, or ``disable`` rule subcommands."""
    if rules_cmd == "list":
        kind_filter = str(getattr(args, "kind", "all") or "all")
        status_filter = str(getattr(args, "status", "all") or "all")
        rows = _collect_rule_rows(
            target_view,
            scope_label=scope_label,
            kind_filter=kind_filter,
            status_filter=status_filter,
        )
        folder_scopes = sorted(
            (getattr(root_settings, "FOLDER_RULE_SCOPES", {}) or {}).keys()
        )
        payload = {
            "status": "success",
            "scope": scope_label,
            "count": len(rows),
            "rules": rows,
            "folder_scopes": folder_scopes,
        }
        if is_json:
            sys.stdout.write(json.dumps(payload, indent=2) + "\n")
            sys.stdout.flush()
        else:
            if not rows:
                if not quiet:
                    print(f"No rules configured for scope '{scope_label}'.")
            else:
                if not quiet:
                    print(f"Rules [{scope_label}] ({len(rows)} total):")
                for row in rows:
                    target_disp = row.get("target") or "(none)"
                    extra = ""
                    if row["kind"] == "policy":
                        conds = row.get("conditions")
                        if isinstance(conds, list) and conds:
                            mode_str = row.get("match_mode", "all")
                            extra = (
                                f" [{row['policy_type']}:{row['action']},"
                                f"mode={mode_str},{len(conds)} clauses]"
                            )
                        else:
                            extra = f" [{row['policy_type']}:{row['action']}]"
                    print(
                        f"  [{row['status']}] [{row['kind']}{extra}] "
                        f"{row['pattern']} -> {target_disp} (priority={row['priority']})"
                    )
        sys.exit(0)

    if rules_cmd == "add":
        pattern = _extract_pattern_arg(args, subcmd="add", json_mode=is_json)
        raw_target = getattr(args, "target_opt", None)
        if raw_target is None:
            raw_target = getattr(args, "target_pos", None)
        target_str = str(raw_target).strip() if raw_target is not None else ""

        kind = str(getattr(args, "kind", "keyword") or "keyword").lower()
        as_disabled = bool(getattr(args, "disabled", False))

        if kind == "extension" and as_disabled:
            emit_cli_error(
                "Error: Extension category rules cannot be added in disabled state.",
                json_mode=is_json,
                exit_code=2,
            )

        if kind in ("keyword", "learned", "extension") and not target_str:
            emit_cli_error(
                f"Error: Missing destination target folder for 'rules add' (--kind={kind}).",
                json_mode=is_json,
                exit_code=2,
            )

        created_entry: dict[str, Any]
        try:
            if kind == "keyword":
                kw_key = pattern.lower()
                active_kw = dict(getattr(target_view, "KEYWORD_RULES", {}) or {})
                disabled_kw = dict(
                    getattr(target_view, "DISABLED_KEYWORD_RULES", {}) or {}
                )
                ex_act = _find_dict_key_case_insensitive(active_kw, kw_key)
                ex_dis = _find_dict_key_case_insensitive(disabled_kw, kw_key)
                if ex_act is not None:
                    active_kw.pop(ex_act, None)
                if ex_dis is not None:
                    disabled_kw.pop(ex_dis, None)
                if as_disabled:
                    disabled_kw[kw_key] = target_str
                else:
                    active_kw[kw_key] = target_str
                target_view.KEYWORD_RULES = active_kw
                target_view.DISABLED_KEYWORD_RULES = disabled_kw
                created_entry = {
                    "kind": "keyword",
                    "pattern": kw_key,
                    "target": target_str,
                    "enabled": not as_disabled,
                    "status": "disabled" if as_disabled else "active",
                    "scope": scope_label,
                }
            elif kind == "learned":
                active_learned = dict(getattr(target_view, "LEARNED_RULES", {}) or {})
                disabled_learned = dict(
                    getattr(target_view, "DISABLED_LEARNED_RULES", {}) or {}
                )
                for k_old in _find_learned_keys(active_learned, pattern):
                    active_learned.pop(k_old, None)
                for k_old in _find_learned_keys(disabled_learned, pattern):
                    disabled_learned.pop(k_old, None)
                if as_disabled:
                    disabled_learned[pattern] = target_str
                else:
                    active_learned[pattern] = target_str
                target_view.LEARNED_RULES = active_learned
                target_view.DISABLED_LEARNED_RULES = disabled_learned
                created_entry = {
                    "kind": "learned",
                    "pattern": pattern,
                    "target": target_str,
                    "enabled": not as_disabled,
                    "status": "disabled" if as_disabled else "active",
                    "scope": scope_label,
                }
            elif kind == "extension":
                ext_tokens: list[str] = []
                for tok in pattern.split(","):
                    t_clean = tok.strip().lower()
                    if not t_clean:
                        continue
                    if not t_clean.startswith("."):
                        t_clean = f".{t_clean}"
                    if t_clean not in ext_tokens:
                        ext_tokens.append(t_clean)
                if not ext_tokens:
                    emit_cli_error(
                        "Error: At least one file extension is required for --kind=extension.",
                        json_mode=is_json,
                        exit_code=2,
                    )
                ext_cats = copy.deepcopy(
                    dict(getattr(target_view, "EXTENSION_CATEGORIES", {}) or {})
                )
                existing_list = ext_cats.get(target_str)
                if isinstance(existing_list, list):
                    merged_exts = list(existing_list)
                    for ext in ext_tokens:
                        if ext not in merged_exts:
                            merged_exts.append(ext)
                    ext_cats[target_str] = merged_exts
                else:
                    ext_cats[target_str] = ext_tokens
                target_view.EXTENSION_CATEGORIES = ext_cats
                created_entry = {
                    "kind": "extension",
                    "pattern": ", ".join(ext_cats[target_str]),
                    "extensions": list(ext_cats[target_str]),
                    "target": target_str,
                    "enabled": True,
                    "status": "active",
                    "scope": scope_label,
                }
            else:
                raw_pol_type = str(
                    getattr(args, "policy_type", "keyword") or "keyword"
                ).strip()
                pol_type = _POLICY_TYPE_ALIASES.get(
                    raw_pol_type.lower(), raw_pol_type.lower()
                )
                pol_action = getattr(args, "policy_action", None)
                allows_empty = (
                    str(pol_action or "").lower()
                    in ("trash", "duplicate", "unarchive", "redact")
                    or pol_type.lower() == "duplicate"
                )
                if not target_str and not allows_empty:
                    emit_cli_error(
                        "Error: Missing destination target folder for policy rule.",
                        json_mode=is_json,
                        exit_code=2,
                    )
                pol_dict: dict[str, Any] = {
                    "type": pol_type,
                    "expression": pattern,
                    "target_path": target_str,
                    "priority": int(getattr(args, "priority", 75)),
                    "halting": bool(getattr(args, "halting", True)),
                    "enabled": not as_disabled,
                }
                if pol_action:
                    pol_dict["action"] = str(pol_action)
                ren_pat = getattr(args, "rename_pattern", None)
                if ren_pat:
                    pol_dict["rename_pattern"] = str(ren_pat).strip()

                policies = [
                    dict(p) for p in (getattr(target_view, "POLICIES", []) or [])
                ]
                replaced = False
                for idx, ex_p in enumerate(policies):
                    if (
                        not ex_p.get("conditions")
                        and str(ex_p.get("type", "")).strip().lower()
                        == pol_type.lower()
                        and str(ex_p.get("expression", "")).strip().lower()
                        == pattern.lower()
                    ):
                        policies[idx] = pol_dict
                        replaced = True
                        break
                if not replaced:
                    policies.append(pol_dict)
                target_view.POLICIES = policies
                created_entry = {
                    "kind": "policy",
                    "policy_type": pol_type,
                    "pattern": pattern,
                    "target": target_str,
                    "action": str(pol_action or "move"),
                    "priority": pol_dict["priority"],
                    "enabled": not as_disabled,
                    "status": "disabled" if as_disabled else "active",
                    "scope": scope_label,
                }

            root_settings.flush()
        except ValueError as exc:
            emit_cli_error(f"Error: {exc}", json_mode=is_json, exit_code=1)

        payload = {
            "status": "added",
            "scope": scope_label,
            "rule": created_entry,
        }
        if is_json:
            sys.stdout.write(json.dumps(payload, indent=2) + "\n")
            sys.stdout.flush()
        elif not quiet:
            print(
                f"Added [{created_entry['status']}] {created_entry['kind']} rule "
                f"'{created_entry['pattern']}' -> '{created_entry['target']}' (scope: {scope_label})."
            )
        sys.exit(0)

    if rules_cmd == "remove":
        pattern = _extract_pattern_arg(args, subcmd="remove", json_mode=is_json)
        kind_filter = str(getattr(args, "kind", "all") or "all").lower()
        removed_items: list[dict[str, Any]] = []

        if kind_filter in ("all", "keyword"):
            active_kw = dict(getattr(target_view, "KEYWORD_RULES", {}) or {})
            disabled_kw = dict(getattr(target_view, "DISABLED_KEYWORD_RULES", {}) or {})
            k_act = _find_dict_key_case_insensitive(active_kw, pattern)
            if k_act is not None:
                tgt = active_kw.pop(k_act)
                target_view.KEYWORD_RULES = active_kw
                removed_items.append(
                    {
                        "kind": "keyword",
                        "pattern": k_act,
                        "target": tgt,
                        "status": "active",
                    }
                )
            k_dis = _find_dict_key_case_insensitive(disabled_kw, pattern)
            if k_dis is not None:
                tgt = disabled_kw.pop(k_dis)
                target_view.DISABLED_KEYWORD_RULES = disabled_kw
                removed_items.append(
                    {
                        "kind": "keyword",
                        "pattern": k_dis,
                        "target": tgt,
                        "status": "disabled",
                    }
                )

        if kind_filter in ("all", "learned"):
            active_learned = dict(getattr(target_view, "LEARNED_RULES", {}) or {})
            disabled_learned = dict(
                getattr(target_view, "DISABLED_LEARNED_RULES", {}) or {}
            )
            matched_act = _find_learned_keys(active_learned, pattern)
            if matched_act:
                for k_l in matched_act:
                    tgt = active_learned.pop(k_l)
                    removed_items.append(
                        {
                            "kind": "learned",
                            "pattern": k_l,
                            "target": tgt,
                            "status": "active",
                        }
                    )
                target_view.LEARNED_RULES = active_learned
            matched_dis = _find_learned_keys(disabled_learned, pattern)
            if matched_dis:
                for k_l in matched_dis:
                    tgt = disabled_learned.pop(k_l)
                    removed_items.append(
                        {
                            "kind": "learned",
                            "pattern": k_l,
                            "target": tgt,
                            "status": "disabled",
                        }
                    )
                target_view.DISABLED_LEARNED_RULES = disabled_learned

        if kind_filter in ("all", "extension"):
            ext_cats = copy.deepcopy(
                dict(getattr(target_view, "EXTENSION_CATEGORIES", {}) or {})
            )
            cat_match = None
            for cat_k in ext_cats:
                if str(cat_k).lower() == pattern.lower():
                    cat_match = cat_k
                    break
            if cat_match is not None:
                old_exts = ext_cats.pop(cat_match)
                target_view.EXTENSION_CATEGORIES = ext_cats
                removed_items.append(
                    {
                        "kind": "extension",
                        "pattern": str(old_exts),
                        "target": cat_match,
                        "status": "active",
                    }
                )
            else:
                norm_ext = (
                    pattern.lower()
                    if pattern.startswith(".")
                    else f".{pattern.lower()}"
                )
                ext_changed = False
                for cat_k in list(ext_cats.keys()):
                    val = ext_cats[cat_k]
                    if isinstance(val, list) and norm_ext in [
                        str(x).lower() for x in val
                    ]:
                        filtered = [x for x in val if str(x).lower() != norm_ext]
                        if filtered:
                            ext_cats[cat_k] = filtered
                        else:
                            ext_cats.pop(cat_k, None)
                        ext_changed = True
                        removed_items.append(
                            {
                                "kind": "extension",
                                "pattern": norm_ext,
                                "target": cat_k,
                                "status": "active",
                            }
                        )
                if ext_changed:
                    target_view.EXTENSION_CATEGORIES = ext_cats

        if kind_filter in ("all", "policy"):
            policies = [dict(p) for p in (getattr(target_view, "POLICIES", []) or [])]
            kept_policies: list[dict[str, Any]] = []
            for pol in policies:
                expr = str(pol.get("expression", "")).strip()
                if expr == pattern or expr.lower() == pattern.lower():
                    removed_items.append(
                        {
                            "kind": "policy",
                            "pattern": expr,
                            "target": str(pol.get("target_path", "")),
                            "status": "active"
                            if pol.get("enabled") is not False
                            else "disabled",
                        }
                    )
                else:
                    kept_policies.append(pol)
            if len(kept_policies) != len(policies):
                target_view.POLICIES = kept_policies

        if not removed_items:
            emit_cli_error(
                f"Error: No matching rule found for '{pattern}' in scope '{scope_label}'.",
                json_mode=is_json,
                exit_code=1,
            )

        root_settings.flush()
        payload = {
            "status": "removed",
            "scope": scope_label,
            "pattern": pattern,
            "removed_count": len(removed_items),
            "removed": removed_items,
        }
        if is_json:
            sys.stdout.write(json.dumps(payload, indent=2) + "\n")
            sys.stdout.flush()
        elif not quiet:
            print(
                f"Removed {len(removed_items)} rule(s) matching '{pattern}' (scope: {scope_label})."
            )
        sys.exit(0)

    if rules_cmd in ("enable", "disable"):
        enable_mode = rules_cmd == "enable"
        pattern = _extract_pattern_arg(args, subcmd=rules_cmd, json_mode=is_json)
        kind_filter = str(getattr(args, "kind", "all") or "all").lower()
        updated_items: list[dict[str, Any]] = []

        if kind_filter in ("all", "keyword"):
            active_kw = dict(getattr(target_view, "KEYWORD_RULES", {}) or {})
            disabled_kw = dict(getattr(target_view, "DISABLED_KEYWORD_RULES", {}) or {})
            if enable_mode:
                k_dis = _find_dict_key_case_insensitive(disabled_kw, pattern)
                if k_dis is not None:
                    tgt = disabled_kw.pop(k_dis)
                    active_kw[k_dis] = tgt
                    target_view.KEYWORD_RULES = active_kw
                    target_view.DISABLED_KEYWORD_RULES = disabled_kw
                    updated_items.append(
                        {
                            "kind": "keyword",
                            "pattern": k_dis,
                            "target": tgt,
                            "enabled": True,
                            "status": "active",
                        }
                    )
                else:
                    k_act = _find_dict_key_case_insensitive(active_kw, pattern)
                    if k_act is not None:
                        updated_items.append(
                            {
                                "kind": "keyword",
                                "pattern": k_act,
                                "target": active_kw[k_act],
                                "enabled": True,
                                "status": "active",
                            }
                        )
            else:
                k_act = _find_dict_key_case_insensitive(active_kw, pattern)
                if k_act is not None:
                    tgt = active_kw.pop(k_act)
                    disabled_kw[k_act] = tgt
                    target_view.KEYWORD_RULES = active_kw
                    target_view.DISABLED_KEYWORD_RULES = disabled_kw
                    updated_items.append(
                        {
                            "kind": "keyword",
                            "pattern": k_act,
                            "target": tgt,
                            "enabled": False,
                            "status": "disabled",
                        }
                    )
                else:
                    k_dis = _find_dict_key_case_insensitive(disabled_kw, pattern)
                    if k_dis is not None:
                        updated_items.append(
                            {
                                "kind": "keyword",
                                "pattern": k_dis,
                                "target": disabled_kw[k_dis],
                                "enabled": False,
                                "status": "disabled",
                            }
                        )

        if kind_filter in ("all", "learned"):
            active_learned = dict(getattr(target_view, "LEARNED_RULES", {}) or {})
            disabled_learned = dict(
                getattr(target_view, "DISABLED_LEARNED_RULES", {}) or {}
            )
            if enable_mode:
                matched_dis = _find_learned_keys(disabled_learned, pattern)
                if matched_dis:
                    for k_l in matched_dis:
                        tgt = disabled_learned.pop(k_l)
                        active_learned[k_l] = tgt
                        updated_items.append(
                            {
                                "kind": "learned",
                                "pattern": k_l,
                                "target": tgt,
                                "enabled": True,
                                "status": "active",
                            }
                        )
                    target_view.LEARNED_RULES = active_learned
                    target_view.DISABLED_LEARNED_RULES = disabled_learned
                else:
                    matched_act = _find_learned_keys(active_learned, pattern)
                    for k_l in matched_act:
                        updated_items.append(
                            {
                                "kind": "learned",
                                "pattern": k_l,
                                "target": active_learned[k_l],
                                "enabled": True,
                                "status": "active",
                            }
                        )
            else:
                matched_act = _find_learned_keys(active_learned, pattern)
                if matched_act:
                    for k_l in matched_act:
                        tgt = active_learned.pop(k_l)
                        disabled_learned[k_l] = tgt
                        updated_items.append(
                            {
                                "kind": "learned",
                                "pattern": k_l,
                                "target": tgt,
                                "enabled": False,
                                "status": "disabled",
                            }
                        )
                    target_view.LEARNED_RULES = active_learned
                    target_view.DISABLED_LEARNED_RULES = disabled_learned
                else:
                    matched_dis = _find_learned_keys(disabled_learned, pattern)
                    for k_l in matched_dis:
                        updated_items.append(
                            {
                                "kind": "learned",
                                "pattern": k_l,
                                "target": disabled_learned[k_l],
                                "enabled": False,
                                "status": "disabled",
                            }
                        )

        if kind_filter in ("all", "policy"):
            policies = [dict(p) for p in (getattr(target_view, "POLICIES", []) or [])]
            pol_changed = False
            for pol in policies:
                expr = str(pol.get("expression", "")).strip()
                if expr == pattern or expr.lower() == pattern.lower():
                    pol["enabled"] = enable_mode
                    pol_changed = True
                    updated_items.append(
                        {
                            "kind": "policy",
                            "pattern": expr,
                            "target": str(pol.get("target_path", "")),
                            "enabled": enable_mode,
                            "status": "active" if enable_mode else "disabled",
                        }
                    )
            if pol_changed:
                target_view.POLICIES = policies

        if not updated_items:
            emit_cli_error(
                f"Error: No matching rule found for '{pattern}' in scope '{scope_label}'.",
                json_mode=is_json,
                exit_code=1,
            )

        root_settings.flush()
        status_verb = "enabled" if enable_mode else "disabled"
        payload = {
            "status": status_verb,
            "scope": scope_label,
            "pattern": pattern,
            "updated_count": len(updated_items),
            "updated": updated_items,
        }
        if is_json:
            sys.stdout.write(json.dumps(payload, indent=2) + "\n")
            sys.stdout.flush()
        elif not quiet:
            print(
                f"{status_verb.capitalize()} {len(updated_items)} rule(s) matching '{pattern}' (scope: {scope_label})."
            )
        sys.exit(0)

    return False
