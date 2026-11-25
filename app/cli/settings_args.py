"""Shared CLI argument registration helper for --settings-file and --profile flags."""

from __future__ import annotations

import argparse


def add_settings_source_args(
    parser: argparse.ArgumentParser,
    *,
    suppress_default: bool = True,
    settings_help: str = "Path to an explicit Sortify settings.json file",
) -> None:
    """Register mutually exclusive ``--settings-file`` and ``--profile`` CLI options."""
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
