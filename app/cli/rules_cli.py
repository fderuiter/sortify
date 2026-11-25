"""CLI subcommand handler for headless rule management, import, and export workflows."""

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

from app.cli.error_emitter import emit_cli_error
from app.config import (
    AppSettings,
    normalize_folder_scope_key,
    resolve_cli_settings_filepath,
)
from app.core.metadata import matches_learned_rule_pattern
from app.core.rule_importers import (
    ImportResult,
    RuleConflict,
    RuleConflictAnalyzer,
    get_rule_importer,
)
from app.core.rule_templates import export_active_rules

_VALID_CONFLICT_STRATEGIES = ("keep_existing", "overwrite", "skip_conflicting")
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


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``rules`` subcommand group for managing, importing, and exporting rules.

    Parameters
    ----------
    subparsers : argparse._SubParsersAction
        Root argument parser subparsers collection.
    """
    parser_rules = subparsers.add_parser(
        "rules",
        help="Manage, import, or export routing rules (global or folder-scoped)",
    )
    parser_rules.add_argument(
        "--json",
        action="store_true",
        help="Output response in structured JSON format",
    )
    rules_settings_group = parser_rules.add_mutually_exclusive_group()
    rules_settings_group.add_argument(
        "--settings-file",
        type=str,
        default=argparse.SUPPRESS,
        help="Optional path to a specific Sortify settings.json file",
    )
    rules_settings_group.add_argument(
        "--profile",
        type=str,
        default=argparse.SUPPRESS,
        help="Named configuration profile under ~/.autosorter/profiles/<name>.json",
    )
    parser_rules.add_argument(
        "--folder",
        type=str,
        default=None,
        help="Scope rule inspection or updates to a specific target directory",
    )
    rules_subparsers = parser_rules.add_subparsers(
        dest="rules_command",
        help="Rule management subcommands",
    )

    def add_rules_common(target_parser: argparse.ArgumentParser) -> None:
        settings_group = target_parser.add_mutually_exclusive_group()
        settings_group.add_argument(
            "--settings-file",
            type=str,
            default=argparse.SUPPRESS,
            help="Optional path to a specific Sortify settings.json file",
        )
        settings_group.add_argument(
            "--profile",
            type=str,
            default=argparse.SUPPRESS,
            help="Named configuration profile under ~/.autosorter/profiles/<name>.json",
        )
        target_parser.add_argument(
            "--folder",
            type=str,
            default=argparse.SUPPRESS,
            help="Scope rule operation to a specific target directory",
        )
        target_parser.add_argument(
            "-q",
            "--quiet",
            action="store_true",
            dest="quiet",
            default=argparse.SUPPRESS,
            help="Suppress non-essential informational output",
        )
        target_parser.add_argument(
            "--no-color",
            action="store_true",
            dest="no_color",
            default=argparse.SUPPRESS,
            help="Disable ANSI color formatting",
        )
        target_parser.add_argument(
            "--json",
            action="store_true",
            help="Output response in structured JSON format",
        )

    p_import = rules_subparsers.add_parser(
        "import",
        help="Parse, preview, and merge rule packages into Sortify settings",
    )
    add_rules_common(p_import)
    p_import.add_argument(
        "file_path",
        nargs="?",
        default=None,
        metavar="FILE",
        help="Path to rule file (.hazelrules, .plist, .ini, .xml, .json, .dropit)",
    )
    p_import.add_argument(
        "-f",
        "--file",
        dest="file_opt",
        type=str,
        default=None,
        help="Path to rule file (.hazelrules, .plist, .ini, .xml, .json, .dropit)",
    )
    p_import.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview extracted rules, path conversions, warnings, and conflicts without saving",
    )
    p_import.add_argument(
        "--conflict-strategy",
        type=str,
        choices=list(_VALID_CONFLICT_STRATEGIES),
        default="keep_existing",
        help="Default resolution strategy for rule collisions (default: keep_existing)",
    )
    p_import.add_argument(
        "--resolve",
        action="append",
        default=[],
        metavar="IDENT=STRATEGY",
        help="Per-rule conflict override, e.g. --resolve invoice=overwrite (repeatable)",
    )
    state_group = p_import.add_mutually_exclusive_group()
    state_group.add_argument(
        "--disabled",
        action="store_true",
        dest="force_disabled",
        help="Import rules disabled for review (default for external formats)",
    )
    state_group.add_argument(
        "--enable",
        "--enabled",
        action="store_true",
        dest="force_enabled",
        help="Activate newly imported rules immediately",
    )

    p_export = rules_subparsers.add_parser(
        "export",
        help="Export rules and policies to Sortify JSON, Hazel, or DropIt INI",
    )
    add_rules_common(p_export)
    p_export.add_argument(
        "output_path",
        nargs="?",
        default=None,
        metavar="OUTPUT",
        help="Destination file path for exported rules",
    )
    p_export.add_argument(
        "-o",
        "--output",
        dest="output_opt",
        type=str,
        default=None,
        help="Destination file path for exported rules",
    )
    p_export.add_argument(
        "-f",
        "--format",
        dest="export_format",
        type=str,
        choices=["sortify_json", "hazel", "dropit_ini", "json", "hazelrules", "ini"],
        default=None,
        help="Target export format (inferred from file extension when omitted)",
    )
    p_export.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview rule counts and target format without writing export file",
    )

    p_list = rules_subparsers.add_parser(
        "list",
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


def _parse_resolution_overrides(
    raw_items: list[str], *, json_mode: bool
) -> dict[str, str]:
    """Parse ``IDENT=STRATEGY`` CLI arguments into a resolution lookup dictionary."""
    overrides: dict[str, str] = {}
    for entry in raw_items:
        if "=" not in entry:
            emit_cli_error(
                f"Error: Invalid --resolve value '{entry}'. Expected IDENT=STRATEGY.",
                json_mode=json_mode,
                exit_code=2,
            )
        ident_part, strat_part = entry.rsplit("=", 1)
        ident_clean = ident_part.strip()
        strat_clean = strat_part.strip().lower()
        if not ident_clean or strat_clean not in _VALID_CONFLICT_STRATEGIES:
            valid = ", ".join(_VALID_CONFLICT_STRATEGIES)
            emit_cli_error(
                f"Error: Invalid resolution '{entry}'. Strategy must be one of: {valid}.",
                json_mode=json_mode,
                exit_code=2,
            )
        overrides[ident_clean] = strat_clean
    return overrides


def _serialize_conflict(
    conflict: RuleConflict,
    overrides: dict[str, str],
    default_strategy: str,
) -> dict[str, Any]:
    """Build a serializable dictionary for a detected rule conflict with its effective resolution."""
    resolved = overrides.get(
        conflict.identifier,
        overrides.get(conflict.identifier.lower(), default_strategy),
    )
    return {
        "rule_type": conflict.rule_type,
        "identifier": conflict.identifier,
        "existing_target": conflict.existing_target,
        "imported_target": conflict.imported_target,
        "existing_priority": conflict.existing_priority,
        "imported_priority": conflict.imported_priority,
        "resolution": resolved,
    }


def _print_human_import_report(
    result: ImportResult,
    conflict_rows: list[dict[str, Any]],
    *,
    dry_run: bool,
    import_disabled: bool,
    force_enable: bool,
    quiet: bool,
    settings: Any,
) -> None:
    """Render a readable CLI report for rule import preview or merge execution."""
    if quiet:
        return

    heading = "[DRY-RUN] Rule Import Preview" if dry_run else "Rule Import Summary"
    if import_disabled:
        stage_note = "disabled for review"
    elif result.preserve_states and not force_enable:
        stage_note = "preserved from backup"
    else:
        stage_note = "enabled immediately"
    print(f"{heading} ({result.source_format})")
    print(f"  New rule staging: {stage_note}")
    print(
        f"  Extracted: {len(result.keyword_rules)} keyword rule(s), "
        f"{len(result.policies)} policy rule(s)"
    )

    if result.keyword_rules:
        print("  Keyword Rules:")
        for kw, target in result.keyword_rules.items():
            print(f"    - {kw} -> {target}")

    if result.policies:
        print("  Policy Rules:")
        for pol in result.policies:
            p_type = pol.get("type", "keyword")
            act = pol.get("action")
            badge = f" [{act}]" if act else ""
            print(
                f"    - ({p_type}{badge}) {pol.get('expression', '')} -> "
                f"{pol.get('target_path', '')} [priority={pol.get('priority', 75)}]"
            )

    if conflict_rows:
        print(f"  Conflicts ({len(conflict_rows)}):")
        for row in conflict_rows:
            print(
                f"    - [{row['rule_type']}] {row['identifier']}: "
                f"existing='{row['existing_target']}' vs imported='{row['imported_target']}' "
                f"-> {row['resolution']}"
            )

    if result.warnings:
        print(f"  Warnings ({len(result.warnings)}):")
        for warn in result.warnings:
            print(f"    ! {warn}")

    if dry_run:
        print("  Dry-run complete: no settings were modified.")
    else:
        active_kw = len(getattr(settings, "KEYWORD_RULES", {}) or {})
        disabled_kw = len(getattr(settings, "DISABLED_KEYWORD_RULES", {}) or {})
        total_pol = len(getattr(settings, "POLICIES", []) or [])
        print(
            f"  Settings updated: {active_kw} active keyword(s), "
            f"{disabled_kw} disabled keyword(s), {total_pol} policy rule(s)."
        )


def _infer_export_format(output_path: str, explicit_format: str | None) -> str:
    """Resolve canonical export format from explicit flag or destination file suffix."""
    if explicit_format:
        fmt_lower = explicit_format.strip().lower()
        if fmt_lower in ("json", "sortify_json"):
            return "sortify_json"
        if fmt_lower in ("hazel", "hazelrules"):
            return "hazel"
        if fmt_lower in ("ini", "dropit_ini"):
            return "dropit_ini"
        return fmt_lower

    suffix = Path(output_path).suffix.lower()
    if suffix in (".hazelrules", ".plist"):
        return "hazel"
    if suffix == ".ini":
        return "dropit_ini"
    return "sortify_json"


def _resolve_rules_target_settings(
    args: argparse.Namespace,
    default_settings: AppSettings,
    *,
    json_mode: bool,
) -> tuple[AppSettings, Any, str | None]:
    """Resolve root ``AppSettings``, optional ``ScopedSettingsView``, and canonical folder key."""
    raw_sf = getattr(args, "settings_file", None)
    raw_prof = getattr(args, "profile", None)
    root_settings = default_settings
    if raw_sf is not None or raw_prof is not None:
        try:
            resolved_fp = resolve_cli_settings_filepath(
                settings_file=raw_sf,
                profile=raw_prof,
            )
        except ValueError as err:
            emit_cli_error(f"Error: {err}", json_mode=json_mode, exit_code=2)
        if resolved_fp:
            curr_fp = os.path.normcase(os.path.abspath(str(default_settings.filepath)))
            want_fp = os.path.normcase(os.path.abspath(resolved_fp))
            if curr_fp != want_fp:
                root_settings = AppSettings(filepath=resolved_fp)

    raw_folder = getattr(args, "folder", None)
    if raw_folder is not None and str(raw_folder).strip():
        try:
            scope_key = normalize_folder_scope_key(str(raw_folder).strip())
        except ValueError as err:
            emit_cli_error(f"Error: {err}", json_mode=json_mode, exit_code=2)
        target_view = root_settings.for_folder(scope_key, fallback_to_global=False)
        return root_settings, target_view, scope_key

    return root_settings, root_settings, None


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
            rows.append(
                {
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
            )

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
    matches = [k for k in mapping if matches_learned_rule_pattern(str(k), query)]
    return matches


def handle_rules_command(args: argparse.Namespace, settings: AppSettings) -> bool:
    """Execute ``rules`` subcommands (``import``, ``export``, ``list``, ``add``, ``remove``, ``enable``, ``disable``).

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI arguments.
    settings : AppSettings
        Active application settings instance.

    Returns
    -------
    bool
        True if the ``rules`` subcommand was handled, False otherwise.
    """
    if getattr(args, "subcommand", None) != "rules":
        return False

    is_json = bool(getattr(args, "json", False))
    quiet = bool(getattr(args, "quiet", False))
    rules_cmd = getattr(args, "rules_command", None)

    if not rules_cmd:
        emit_cli_error(
            "Error: Missing rules subcommand. Use 'list', 'add', 'remove', 'enable', 'disable', 'import', or 'export'.",
            json_mode=is_json,
            exit_code=2,
        )

    root_settings, target_view, scope_key = _resolve_rules_target_settings(
        args,
        settings,
        json_mode=is_json,
    )
    scope_label = scope_key or "global"

    if rules_cmd == "import":
        raw_file = getattr(args, "file_opt", None) or getattr(args, "file_path", None)
        if not raw_file or not str(raw_file).strip():
            emit_cli_error(
                "Error: Missing rule file path for 'rules import'.",
                json_mode=is_json,
                exit_code=2,
            )

        rule_path = Path(str(raw_file).strip()).expanduser()
        if not rule_path.exists() or not rule_path.is_file():
            emit_cli_error(
                f"Error: Rule file not found: '{rule_path}'",
                json_mode=is_json,
                exit_code=1,
            )

        dry_run = bool(getattr(args, "dry_run", False))
        default_strategy = str(getattr(args, "conflict_strategy", "keep_existing"))
        raw_resolves = list(getattr(args, "resolve", None) or [])
        overrides = _parse_resolution_overrides(raw_resolves, json_mode=is_json)
        force_disabled = bool(getattr(args, "force_disabled", False))
        force_enabled = bool(getattr(args, "force_enabled", False))

        try:
            importer = get_rule_importer(rule_path)
            import_res = importer.parse_file(rule_path)
        except (ValueError, OSError) as exc:
            emit_cli_error(
                f"Error: {exc}",
                json_mode=is_json,
                exit_code=1,
            )

        if force_disabled:
            import_disabled = True
            force_enable = False
        elif force_enabled:
            import_disabled = False
            force_enable = True
        elif import_res.preserve_states:
            import_disabled = False
            force_enable = False
        else:
            import_disabled = True
            force_enable = False

        has_any_rules = bool(
            import_res.keyword_rules
            or import_res.disabled_keyword_rules
            or import_res.learned_rules
            or import_res.disabled_learned_rules
            or import_res.extension_categories
            or import_res.policies
            or import_res.folder_rule_scopes
        )
        has_fatal_parse_error = (
            not has_any_rules
            and not import_res.unsupported
            and any(
                w.startswith(
                    ("Failed to parse", "Failed to read", "Invalid ", "Unrecognized ")
                )
                for w in import_res.warnings
            )
        )
        if has_fatal_parse_error:
            emit_cli_error(
                f"Error: {import_res.warnings[0]}",
                json_mode=is_json,
                exit_code=1,
            )

        conflicts = RuleConflictAnalyzer.analyze(import_res, target_view)
        conflict_rows = [
            _serialize_conflict(c, overrides, default_strategy) for c in conflicts
        ]

        effective_resolutions: dict[str, str] = dict(overrides)
        for c in conflicts:
            if (
                c.identifier not in effective_resolutions
                and c.identifier.lower() in overrides
            ):
                effective_resolutions[c.identifier] = overrides[c.identifier.lower()]

        if not dry_run:
            try:
                import_res.merge_into_settings(
                    target_view,
                    resolutions=effective_resolutions,
                    default_strategy=default_strategy,
                    import_disabled=import_disabled,
                    force_enable=force_enable,
                )
                root_settings.flush()
            except (OSError, ValueError) as exc:
                emit_cli_error(
                    f"Error saving imported rules: {exc}",
                    json_mode=is_json,
                    exit_code=1,
                )

        summary: dict[str, Any] = {
            "extracted_keywords": len(import_res.keyword_rules),
            "extracted_disabled_keywords": len(import_res.disabled_keyword_rules),
            "extracted_learned_rules": len(import_res.learned_rules),
            "extracted_disabled_learned_rules": len(import_res.disabled_learned_rules),
            "extracted_extension_categories": len(import_res.extension_categories),
            "extracted_policies": len(import_res.policies),
            "extracted_folder_scopes": len(import_res.folder_rule_scopes),
            "conflicts": len(conflict_rows),
            "warnings": len(import_res.warnings),
            "unsupported": len(import_res.unsupported),
        }
        if not dry_run:
            summary["active_keyword_count"] = len(
                getattr(target_view, "KEYWORD_RULES", {}) or {}
            )
            summary["disabled_keyword_count"] = len(
                getattr(target_view, "DISABLED_KEYWORD_RULES", {}) or {}
            )
            summary["learned_rule_count"] = len(
                getattr(target_view, "LEARNED_RULES", {}) or {}
            )
            summary["disabled_learned_count"] = len(
                getattr(target_view, "DISABLED_LEARNED_RULES", {}) or {}
            )
            summary["extension_category_count"] = len(
                getattr(target_view, "EXTENSION_CATEGORIES", {}) or {}
            )
            summary["policy_count"] = len(getattr(target_view, "POLICIES", []) or [])
            summary["folder_scope_count"] = len(
                getattr(root_settings, "FOLDER_RULE_SCOPES", {}) or {}
            )

        payload = {
            "status": "preview" if dry_run else "merged",
            "dry_run": dry_run,
            "scope": scope_label,
            "import_disabled": import_disabled,
            "preserve_states": import_res.preserve_states,
            "source_format": import_res.source_format,
            "keyword_rules": import_res.keyword_rules,
            "disabled_keyword_rules": import_res.disabled_keyword_rules,
            "learned_rules": import_res.learned_rules,
            "disabled_learned_rules": import_res.disabled_learned_rules,
            "extension_categories": import_res.extension_categories,
            "policies": import_res.policies,
            "folder_rule_scopes": import_res.folder_rule_scopes,
            "conflicts": conflict_rows,
            "warnings": import_res.warnings,
            "unsupported": import_res.unsupported,
            "summary": summary,
        }

        if is_json:
            sys.stdout.write(json.dumps(payload, indent=2) + "\n")
            sys.stdout.flush()
        else:
            _print_human_import_report(
                import_res,
                conflict_rows,
                dry_run=dry_run,
                import_disabled=import_disabled,
                force_enable=force_enable,
                quiet=quiet,
                settings=target_view,
            )

        sys.exit(0)

    if rules_cmd == "export":
        raw_out = getattr(args, "output_opt", None) or getattr(
            args, "output_path", None
        )
        if not raw_out or not str(raw_out).strip():
            emit_cli_error(
                "Error: Missing destination file path for 'rules export'.",
                json_mode=is_json,
                exit_code=2,
            )

        out_str = str(raw_out).strip()
        resolved_fmt = _infer_export_format(
            out_str, getattr(args, "export_format", None)
        )
        dry_run = bool(getattr(args, "dry_run", False))

        kw_count = len(getattr(target_view, "KEYWORD_RULES", {}) or {})
        pol_count = len(getattr(target_view, "POLICIES", []) or [])

        if dry_run:
            dest_preview = str(Path(out_str).expanduser())
            payload = {
                "status": "preview",
                "dry_run": True,
                "scope": scope_label,
                "format": resolved_fmt,
                "output_path": dest_preview,
                "keyword_count": kw_count,
                "policy_count": pol_count,
            }
            if is_json:
                sys.stdout.write(json.dumps(payload, indent=2) + "\n")
                sys.stdout.flush()
            elif not quiet:
                print(
                    f"[DRY-RUN] Would export {kw_count} keyword rule(s) and "
                    f"{pol_count} policy rule(s) as '{resolved_fmt}' to '{dest_preview}'."
                )
            sys.exit(0)

        try:
            written_path = export_active_rules(
                root_settings,
                resolved_fmt,
                out_str,
                folder=scope_key,
            )
        except (ValueError, OSError) as exc:
            emit_cli_error(
                f"Error exporting rules: {exc}",
                json_mode=is_json,
                exit_code=1,
            )

        payload = {
            "status": "exported",
            "dry_run": False,
            "scope": scope_label,
            "format": resolved_fmt,
            "output_path": str(written_path),
            "keyword_count": kw_count,
            "policy_count": pol_count,
        }
        if is_json:
            sys.stdout.write(json.dumps(payload, indent=2) + "\n")
            sys.stdout.flush()
        elif not quiet:
            print(
                f"Exported {kw_count} keyword rule(s) and {pol_count} policy rule(s) "
                f"({resolved_fmt}) to '{written_path}'."
            )
        sys.exit(0)

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
                    extra = (
                        f" [{row['policy_type']}:{row['action']}]"
                        if row["kind"] == "policy"
                        else ""
                    )
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
                ext_tokens = []
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
                        str(ex_p.get("type", "")).strip().lower() == pol_type.lower()
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
