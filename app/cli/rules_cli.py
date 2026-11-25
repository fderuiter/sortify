"""CLI subcommand handler for headless rule management, import, and export workflows."""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from app.cli.error_emitter import emit_cli_error
from app.cli.rules_crud import (
    handle_rules_crud_subcommand,
    register_crud_subparsers,
)
from app.cli.settings_args import add_settings_source_args
from app.config import (
    AppSettings,
    normalize_folder_scope_key,
    resolve_cli_settings_filepath,
)
from app.core.rule_importers import (
    ImportResult,
    RuleConflict,
    RuleConflictAnalyzer,
    get_rule_importer,
)
from app.core.rule_templates import export_active_rules

_VALID_CONFLICT_STRATEGIES = ("keep_existing", "overwrite", "skip_conflicting")


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``rules`` subcommand group for importing and exporting rule packages.

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
    add_settings_source_args(
        parser_rules,
        suppress_default=True,
        settings_help="Optional path to a specific Sortify settings.json file",
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
        add_settings_source_args(
            target_parser,
            suppress_default=True,
            settings_help="Optional path to a specific Sortify settings.json file",
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
        help="Parse, preview, and merge external rule packages into Sortify settings",
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
        help="Import new rules disabled for review (default)",
    )
    state_group.add_argument(
        "--enable",
        "--enabled",
        action="store_true",
        dest="force_enabled",
        help="Activate newly imported rules immediately instead of staging disabled",
    )

    p_export = rules_subparsers.add_parser(
        "export",
        help="Export active keyword rules and policies to Sortify JSON, Hazel, or DropIt INI",
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

    register_crud_subparsers(rules_subparsers, add_rules_common)


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
            badges: list[str] = []
            if act:
                badges.append(str(act))
            conds = pol.get("conditions")
            if isinstance(conds, list) and conds:
                badges.append(f"mode={pol.get('match_mode', 'all')}")
                badges.append(f"{len(conds)} clauses")
            elif pol.get("match_mode"):
                badges.append(f"mode={pol['match_mode']}")
            if pol.get("negate"):
                badges.append("negate")
            if pol.get("regex"):
                badges.append("regex")
            if pol.get("case_sensitive"):
                badges.append("case-sensitive")
            badge_str = "".join(f" [{b}]" for b in badges)
            print(
                f"    - ({p_type}{badge_str}) {pol.get('expression', '')} -> "
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

    return handle_rules_crud_subcommand(
        rules_cmd,
        args,
        root_settings=root_settings,
        target_view=target_view,
        scope_label=scope_label,
        is_json=is_json,
        quiet=quiet,
    )
