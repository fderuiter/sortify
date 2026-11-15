#!/usr/bin/env python3
"""CLI tool for testing ML extraction and analysis in an isolated sandbox environment."""

import argparse
import contextlib
import logging
import os
import shutil
import sys

from app.core.extractor import extract_file_text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SANDBOX_DIR = os.path.join(BASE_DIR, "sandbox", "dataset")
GOLDEN_DIR = os.path.join(BASE_DIR, "sandbox", "dataset_golden")
SANDBOX_DB_PATH = os.path.join(BASE_DIR, "sandbox", "sandbox.db")

logger = logging.getLogger(__name__)


def reset_sandbox():
    """Restores the sandbox dataset to its original state from the golden dataset."""
    try:
        from app.core.db_conn import clear_connection_cache

        clear_connection_cache(only_current_and_inactive=False)
    except Exception:
        logger.debug("Could not clear DB connection cache", exc_info=True)

    db_path = SANDBOX_DB_PATH
    try:
        from app.core.path_utils import resolve_db_crypto

        crypto = resolve_db_crypto(db_path)
        if crypto.isolated_key_path.exists():
            crypto.isolated_key_path.unlink()
    except Exception:
        logger.debug("Could not remove sandbox isolated key", exc_info=True)
    if os.path.exists(SANDBOX_DIR):
        shutil.rmtree(SANDBOX_DIR)
    if os.path.exists(SANDBOX_DB_PATH):
        with contextlib.suppress(OSError):
            os.remove(SANDBOX_DB_PATH)
    for ext in ("-wal", "-shm"):
        sidecar = f"{SANDBOX_DB_PATH}{ext}"
        if os.path.exists(sidecar):
            with contextlib.suppress(OSError):
                os.remove(sidecar)
    state_dir = os.path.join(os.path.dirname(SANDBOX_DB_PATH), "pipeline-state")
    if os.path.isdir(state_dir):
        shutil.rmtree(state_dir)
    shutil.copytree(GOLDEN_DIR, SANDBOX_DIR)
    print("Sandbox dataset has been reset to its original state.", file=sys.stderr)


def extract_file(filename):
    """Extract text from a specific sandbox file."""
    filepath = os.path.join(SANDBOX_DIR, filename)
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}", file=sys.stderr)
        sys.exit(1)
    text = extract_file_text(filepath)
    print(f"--- Extracted Text for {filename} ---", file=sys.stderr)
    print(text)
    print("-" * 40, file=sys.stderr)


def analyze_all(json_output=False):
    """Run the analysis pipeline on all sandbox files."""
    if not os.path.exists(SANDBOX_DIR):
        print("Sandbox dataset not found. Run reset first.", file=sys.stderr)
        sys.exit(1)

    from app.config import Settings
    from app.core.pipeline import SortPipeline
    from app.core.session import AppSession
    from app.core.store import open_store

    settings = Settings(
        _env_file=None,
        AI_CONSENT_GRANTED=False,
        AI_ASSISTED_NAMING=False,
        SORTING_STRATEGY="default",
        MAX_WORKERS=1,
        MAX_FOLDERS=5,
        STOP_WORDS={"the", "and", "a", "an", "is"},
    )
    # Development analysis must never import sessions from the user's store.
    from pathlib import Path

    state_dir = Path(SANDBOX_DB_PATH).parent / "pipeline-state"
    store = open_store(state_dir, legacy_session_root=state_dir / "no-legacy-sessions")
    session = AppSession(settings, base_dir=SANDBOX_DIR, store=store)
    try:

        def progress_callback(*args, **kwargs):
            print("Progress update: File extraction complete.", file=sys.stderr)

        items = [
            f
            for f in os.listdir(SANDBOX_DIR)
            if os.path.isfile(os.path.join(SANDBOX_DIR, f))
            and not f.endswith((".db", ".db-wal", ".db-shm", ".key", ".log"))
            and f != "secret.key"
        ]
        plan = SortPipeline(session).plan(
            items, progress_callback=progress_callback, chunk_size=50
        )
    finally:
        session.close()

    import json

    serializable_plan = (
        plan.model_dump(mode="json") if hasattr(plan, "model_dump") else plan
    )

    if json_output:
        print(json.dumps(serializable_plan, indent=2))
    else:
        print("--- Analysis Sorting Plan ---", file=sys.stderr)
        print(json.dumps(serializable_plan, indent=2))
        print("-" * 40, file=sys.stderr)


def build_parser():
    """Build and return argument parser for sandbox CLI tool."""
    parser = argparse.ArgumentParser(
        prog="sandbox_cli.py",
        description="Sandbox CLI Tool for ML Accuracy Verification",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        dest="quiet",
        help="Suppress informational prints",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        dest="no_color",
        help="Disable ANSI color output",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Launch full-screen Textual TUI mode on sandbox dataset",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Launch full-screen interactive TUI mode on sandbox dataset",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # reset command
    subparsers.add_parser("reset", help="Reset the sandbox dataset to its golden state")

    # extract command
    parser_extract = subparsers.add_parser(
        "extract", help="Extract text from a specific sandbox file"
    )
    parser_extract.add_argument(
        "filename", type=str, help="Name of the file in the sandbox dataset"
    )

    # analyze command
    parser_analyze = subparsers.add_parser(
        "analyze", help="Run the analysis pipeline on all sandbox files"
    )
    parser_analyze.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        dest="quiet",
        default=argparse.SUPPRESS,
        help="Suppress informational prints",
    )
    parser_analyze.add_argument(
        "--no-color",
        action="store_true",
        dest="no_color",
        default=argparse.SUPPRESS,
        help="Disable ANSI color output",
    )
    parser_analyze.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON without decorative borders",
    )
    parser_analyze.add_argument(
        "--tui",
        action="store_true",
        help="Launch full-screen Textual TUI mode on sandbox dataset",
    )
    parser_analyze.add_argument(
        "--interactive",
        action="store_true",
        help="Launch full-screen interactive TUI mode on sandbox dataset",
    )
    return parser


def main():
    """Execute the main CLI logic for the sandbox tool."""
    parser = build_parser()
    args = parser.parse_args()

    if getattr(args, "tui", False) or getattr(args, "interactive", False):
        from app.config import AppSettings
        from app.ui.tui import run_tui

        settings = AppSettings()
        run_tui(settings, SANDBOX_DIR)
        sys.exit(0)

    if args.command == "reset":
        reset_sandbox()
    elif args.command == "extract":
        extract_file(args.filename)
    elif args.command == "analyze":
        analyze_all(json_output=args.json)
    else:
        parser.print_help(file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
