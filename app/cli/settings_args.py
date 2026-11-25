"""Shared CLI parent parsers, subcommand scope rules, and argument helpers."""

from __future__ import annotations

import argparse

_DEFAULT_SETTINGS_HELP = "Path to an explicit Sortify settings.json file"

SORT_OVERRIDE_SUBCOMMANDS: frozenset[str] = frozenset(
    {"sort", "scan", "drop", "daemon"}
)


def should_apply_sort_overrides(args: argparse.Namespace) -> bool:
    """Return ``True`` when the active CLI invocation accepts transient sort overrides."""
    subcmd = getattr(args, "subcommand", None)
    return subcmd is None or subcmd in SORT_OVERRIDE_SUBCOMMANDS


def validate_cross_level_cli_flags(
    parser: argparse.ArgumentParser,
    ns: argparse.Namespace,
) -> None:
    """Enforce cross-level mutual exclusion and sort-override subcommand scoping."""
    if bool(getattr(ns, "accept_ai_consent", False)) and bool(
        getattr(ns, "decline_ai_consent", False)
    ):
        parser.error(
            "argument --decline-ai-consent: not allowed with argument --accept-ai-consent"
        )

    subcmd = getattr(ns, "subcommand", None)
    if subcmd is not None and subcmd not in SORT_OVERRIDE_SUBCOMMANDS:
        if getattr(ns, "duplicates", None) is not None:
            parser.error(
                f"unrecognized arguments: --duplicates (not supported with '{subcmd}')"
            )
        if getattr(ns, "detect_original_by", None) is not None:
            parser.error(
                f"unrecognized arguments: --detect-original-by (not supported with '{subcmd}')"
            )


def add_settings_source_args(
    parser: argparse.ArgumentParser,
    *,
    suppress_default: bool = True,
    settings_help: str = _DEFAULT_SETTINGS_HELP,
) -> None:
    """Register mutually exclusive ``--settings-file`` and ``--profile`` CLI options.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        Target argument parser receiving the settings-source group.
    suppress_default : bool, optional
        When ``True``, uses ``argparse.SUPPRESS`` so child subparsers do not
        overwrite values parsed on a parent parser.
    settings_help : str, optional
        Help text displayed for ``--settings-file``.
    """
    default_val = argparse.SUPPRESS if suppress_default else None
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--settings-file",
        type=str,
        default=default_val,
        help=settings_help,
    )
    group.add_argument(
        "--profile",
        type=str,
        default=default_val,
        help="Named configuration profile under ~/.autosorter/profiles/<name>.json",
    )


def build_global_subparser_parent(
    *,
    settings_help: str = _DEFAULT_SETTINGS_HELP,
) -> argparse.ArgumentParser:
    """Build a fresh parent parser carrying suppressed global CLI flags for subparsers.

    All global flags use ``default=argparse.SUPPRESS`` so flags supplied before a
    top-level or nested subcommand are preserved rather than overwritten by child
    subparser defaults, while the root parser retains deterministic defaults.

    Parameters
    ----------
    settings_help : str, optional
        Help text displayed for ``--settings-file``.

    Returns
    -------
    argparse.ArgumentParser
        Parent parser with ``add_help=False`` ready for ``parents=[...]``.
    """
    parent = argparse.ArgumentParser(add_help=False)
    add_settings_source_args(
        parent,
        suppress_default=True,
        settings_help=settings_help,
    )
    parent.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        dest="quiet",
        default=argparse.SUPPRESS,
        help="Suppress informational prints and non-essential progress output",
    )
    parent.add_argument(
        "--no-color",
        action="store_true",
        dest="no_color",
        default=argparse.SUPPRESS,
        help="Disable ANSI color and style formatting",
    )
    parent.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Output response in structured JSON format",
    )
    parent.add_argument(
        "--tui",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Launch full-screen Textual TUI interface",
    )
    parent.add_argument(
        "--interactive",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Launch full-screen interactive TUI mode",
    )
    ai_consent_group = parent.add_mutually_exclusive_group()
    ai_consent_group.add_argument(
        "--accept-ai-consent",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Pre-configure AI consent as granted and bypass onboarding wizard",
    )
    ai_consent_group.add_argument(
        "--decline-ai-consent",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Pre-configure AI consent as declined and bypass onboarding wizard",
    )
    parent.add_argument(
        "--skip-wizard",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Bypass onboarding wizard modal during startup",
    )
    parent.add_argument(
        "--non-interactive",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Run in non-interactive mode and bypass interactive modal dialogs",
    )
    return parent


def build_sort_override_parent() -> argparse.ArgumentParser:
    """Build a fresh parent parser carrying transient sort-override flags.

    Intended strictly for sorting/triage commands (``sort``, ``scan``, ``drop``,
    and ``daemon``) so non-sorting commands (``config``, ``undo``, ``ledger``,
    ``quarantine``, ``model``, and ``rules``) omit and reject transient sort
    overrides.

    Returns
    -------
    argparse.ArgumentParser
        Parent parser with ``add_help=False`` ready for ``parents=[...]``.
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--max-folders",
        type=int,
        default=None,
        help="Maximum number of generated subfolders",
    )
    parent.add_argument(
        "--strategy",
        type=str,
        choices=["default", "generative", "clinical_tmf", "clinical_isf"],
        default=None,
        help="Sorting strategy",
    )
    parent.add_argument(
        "--conflict-policy",
        type=str,
        choices=["skip", "rename", "overwrite"],
        default=None,
        help="Conflict resolution policy",
    )
    parent.add_argument(
        "--duplicates",
        type=str,
        choices=["off", "review", "skip", "stage", "trash"],
        default=argparse.SUPPRESS,
        help="Byte-identical duplicate handling mode (off, review, skip, stage, trash; review requires --tui or --interactive)",
    )
    parent.add_argument(
        "--detect-original-by",
        type=str,
        choices=[
            "first_seen",
            "-first_seen",
            "name",
            "-name",
            "created",
            "-created",
            "lastmodified",
            "-lastmodified",
        ],
        default=argparse.SUPPRESS,
        help="Criterion used to select the surviving original in duplicate clusters",
    )
    parent.add_argument(
        "--contextual-renaming",
        action="store_true",
        default=None,
        help="Enable AI contextual renaming",
    )
    parent.add_argument(
        "--no-contextual-renaming",
        action="store_false",
        dest="contextual_renaming",
        default=argparse.SUPPRESS,
        help="Disable AI contextual renaming",
    )
    parent.add_argument(
        "--export-report",
        type=str,
        default=None,
        help="Export dry-run simulation report to output file path",
    )
    parent.add_argument(
        "--report-format",
        type=str,
        choices=["html", "json"],
        default=None,
        help="Format for exported simulation report (html or json)",
    )
    return parent
