#!/usr/bin/env python3
"""CLI tool for testing ML extraction and analysis in an isolated sandbox environment."""

import argparse
import contextlib
import json
import logging
import os
import shutil
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

from app.core.extractor import extract_file_text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SANDBOX_DIR = os.path.join(BASE_DIR, "sandbox", "dataset")
GOLDEN_DIR = os.path.join(BASE_DIR, "sandbox", "dataset_golden")
SANDBOX_DB_PATH = os.path.join(BASE_DIR, "sandbox", "sandbox.db")

logger = logging.getLogger(__name__)


def _sandbox_state_dir() -> Path:
    """Return the isolated sandbox runtime state directory."""
    return Path(SANDBOX_DB_PATH).resolve().parent / "pipeline-state"


def _sandbox_keys_dir() -> Path:
    """Return the isolated sandbox fallback key directory."""
    return _sandbox_state_dir() / "keys"


@contextlib.contextmanager
def _isolated_sandbox_env() -> Generator[Path, None, None]:
    """Scope app state, fallback keys, and log output to the sandbox state dir."""
    from app.log_filter import LogScrubbingFilter

    state_dir = _sandbox_state_dir()
    keys_dir = _sandbox_keys_dir()
    prev_app_dir = os.environ.get("AUTOSORTER_APP_DIR")
    prev_keys_dir = os.environ.get("SORTIFY_KEYS_DIR")
    os.environ["AUTOSORTER_APP_DIR"] = str(state_dir)
    os.environ["SORTIFY_KEYS_DIR"] = str(keys_dir)

    root_logger = logging.getLogger()
    added_handler: logging.FileHandler | None = None
    if not root_logger.handlers:
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            log_file = state_dir / "sandbox.log"
            added_handler = logging.FileHandler(log_file, encoding="utf-8")
            added_handler.setLevel(logging.INFO)
            added_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s"
                )
            )
            added_handler.addFilter(LogScrubbingFilter(str(Path.home())))
            root_logger.addHandler(added_handler)
            if root_logger.level == logging.NOTSET or root_logger.level > logging.INFO:
                root_logger.setLevel(logging.INFO)
        except OSError as log_err:
            logger.debug(
                f"Could not attach sandbox log file handler: {log_err}", exc_info=True
            )

    try:
        yield state_dir
    finally:
        if added_handler is not None:
            root_logger.removeHandler(added_handler)
            with contextlib.suppress(OSError):
                added_handler.close()
        if prev_app_dir is None:
            os.environ.pop("AUTOSORTER_APP_DIR", None)
        else:
            os.environ["AUTOSORTER_APP_DIR"] = prev_app_dir
        if prev_keys_dir is None:
            os.environ.pop("SORTIFY_KEYS_DIR", None)
        else:
            os.environ["SORTIFY_KEYS_DIR"] = prev_keys_dir


def _ensure_sandbox_seeded(quiet: bool = False) -> None:
    """Seed the sandbox dataset from the golden dataset when missing."""
    if os.path.exists(SANDBOX_DIR):
        return
    if os.path.isdir(GOLDEN_DIR):
        parent_dir = os.path.dirname(os.path.abspath(SANDBOX_DIR))
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        shutil.copytree(GOLDEN_DIR, SANDBOX_DIR)
        if not quiet:
            print(
                "Initialized sandbox dataset from golden dataset.",
                file=sys.stderr,
            )
        return
    print("Sandbox dataset not found. Run reset first.", file=sys.stderr)
    sys.exit(1)


def _delete_sandbox_keys() -> None:
    """Remove isolated and sidecar `.key` files associated with sandbox databases."""
    from app.core.crypto import CryptoManager, secure_delete_file

    state_dir = _sandbox_state_dir()
    sandbox_root = Path(SANDBOX_DB_PATH).resolve().parent
    candidate_dbs = (
        Path(SANDBOX_DB_PATH),
        state_dir / "sortify.db",
        state_dir / "history.db",
        state_dir / "transaction_ledger.db",
    )
    for candidate_db in candidate_dbs:
        for legacy_md5 in (False, True):
            try:
                key_file = CryptoManager.derive_isolated_key_path(
                    candidate_db, legacy_md5=legacy_md5
                )
                if key_file.exists():
                    secure_delete_file(key_file)
            except OSError as err:
                logger.debug(
                    f"Could not delete isolated key for '{candidate_db}': {err}",
                    exc_info=True,
                )

    for key_dir in (_sandbox_keys_dir(), sandbox_root, Path(SANDBOX_DIR)):
        if key_dir.is_dir():
            for key_file in key_dir.glob("*.key"):
                with contextlib.suppress(OSError):
                    secure_delete_file(key_file)


def reset_sandbox(quiet: bool = False) -> None:
    """Restore the sandbox dataset to its original state from the golden dataset."""
    from app.core.db_conn import clear_connection_cache

    clear_connection_cache(only_current_and_inactive=False)
    _delete_sandbox_keys()
    with _isolated_sandbox_env():
        _delete_sandbox_keys()

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
    state_dir = _sandbox_state_dir()
    if state_dir.is_dir():
        shutil.rmtree(state_dir)
    clear_connection_cache(only_current_and_inactive=False)

    if os.path.isdir(GOLDEN_DIR):
        parent_dir = os.path.dirname(os.path.abspath(SANDBOX_DIR))
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        shutil.copytree(GOLDEN_DIR, SANDBOX_DIR)
    if not quiet:
        print(
            "Sandbox dataset has been reset to its original state.",
            file=sys.stderr,
        )


def extract_file(filename: str, quiet: bool = False) -> None:
    """Extract text from a specific sandbox file."""
    _ensure_sandbox_seeded(quiet=quiet)
    filepath = os.path.join(SANDBOX_DIR, filename)
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}", file=sys.stderr)
        sys.exit(1)
    with _isolated_sandbox_env():
        text = extract_file_text(filepath)
    if not quiet:
        print(f"--- Extracted Text for {filename} ---", file=sys.stderr)
    print(text)
    if not quiet:
        print("-" * 40, file=sys.stderr)


def analyze_all(json_output: bool = False, quiet: bool = False) -> None:
    """Run the analysis pipeline on all sandbox files."""
    _ensure_sandbox_seeded(quiet=quiet)

    from app.config import Settings
    from app.core.db_conn import clear_connection_cache
    from app.core.pipeline import SortPipeline
    from app.core.session import AppSession
    from app.core.store import open_store

    with _isolated_sandbox_env() as state_dir:
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
        store = open_store(
            state_dir, legacy_session_root=state_dir / "no-legacy-sessions"
        )
        session = AppSession(settings, base_dir=SANDBOX_DIR, store=store)
        try:

            def progress_callback(*args: Any, **kwargs: Any) -> None:
                if not quiet:
                    print(
                        "Progress update: File extraction complete.",
                        file=sys.stderr,
                    )

            items = [
                f
                for f in os.listdir(SANDBOX_DIR)
                if os.path.isfile(os.path.join(SANDBOX_DIR, f))
                and not f.endswith((".db", ".db-wal", ".db-shm", ".key", ".log"))
                and f != "secret.key"
            ]
            plan: Any = SortPipeline(session).plan(
                items, progress_callback=progress_callback, chunk_size=50
            )
        finally:
            session.close()
            clear_connection_cache(only_current_and_inactive=False)

    serializable_plan = (
        plan.model_dump(mode="json") if hasattr(plan, "model_dump") else plan
    )

    if json_output or quiet:
        print(json.dumps(serializable_plan, indent=2))
    else:
        print("--- Analysis Sorting Plan ---", file=sys.stderr)
        print(json.dumps(serializable_plan, indent=2))
        print("-" * 40, file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
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


def main() -> None:
    """Execute the main CLI logic for the sandbox tool."""
    parser = build_parser()
    args = parser.parse_args()

    quiet = bool(getattr(args, "quiet", False))
    no_color = bool(getattr(args, "no_color", False) or os.environ.get("NO_COLOR"))

    if no_color:
        from app.main import ANSIStrippingWriter

        os.environ.setdefault("NO_COLOR", "1")
        if sys.stdout is not None and not isinstance(sys.stdout, ANSIStrippingWriter):
            sys.stdout = ANSIStrippingWriter(sys.stdout)
        if sys.stderr is not None and not isinstance(sys.stderr, ANSIStrippingWriter):
            sys.stderr = ANSIStrippingWriter(sys.stderr)

    if getattr(args, "tui", False) or getattr(args, "interactive", False):
        _ensure_sandbox_seeded(quiet=quiet)
        with _isolated_sandbox_env():
            from app.config import AppSettings
            from app.ui.tui import run_tui

            settings = AppSettings()
            try:
                run_tui(settings, SANDBOX_DIR)
            finally:
                save_timer = settings._save_timer
                if save_timer is not None:
                    save_timer.cancel()
                AppSettings._cancel_file_timer(settings._filepath)
        sys.exit(0)

    if args.command == "reset":
        reset_sandbox(quiet=quiet)
    elif args.command == "extract":
        extract_file(args.filename, quiet=quiet)
    elif args.command == "analyze":
        analyze_all(json_output=bool(args.json), quiet=quiet)
    else:
        parser.print_help(file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
