"""Entry point for the Smart AutoSorter AI Pro application.

This script imports and runs the main application GUI or CLI demo.
"""

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, NoReturn

from app.cli.error_emitter import emit_cli_error
from app.cli.settings_args import (
    add_settings_source_args,
    build_global_subparser_parent,
    build_sort_override_parent,
    should_apply_sort_overrides,
    validate_cross_level_cli_flags,
)
from app.config import AppSettings
from app.core.domain_contracts import _make_json_serializable
from app.core.path_utils import is_packaged
from app.core.pipeline import reroot_plan
from app.core.plan_format import (
    count_plan_actions,
    format_plan_tree,
    validate_duplicate_mode_for_headless,
)
from app.core.session_catalog import list_sessions
from app.frozen_runtime import prepare_frozen_windows_runtime
from app.log_filter import LogScrubbingFilter

if not hasattr(AppSettings, "NON_INTERACTIVE"):
    _orig_appsettings_setattr = AppSettings.__setattr__

    def _patched_appsettings_setattr(self, name, value):
        if name == "NON_INTERACTIVE":
            object.__setattr__(self, "_non_interactive", bool(value))
            return
        if name in ("_non_interactive", "_skip_wizard"):
            object.__setattr__(self, name, value)
            return
        _orig_appsettings_setattr(self, name, value)

    AppSettings.__setattr__ = _patched_appsettings_setattr

    setattr(
        AppSettings,
        "NON_INTERACTIVE",
        property(
            lambda self: bool(
                getattr(self, "_non_interactive", False)
                or (
                    os.environ.get("NON_INTERACTIVE", "").strip().lower()
                    in ("1", "true", "yes", "on")
                )
            ),
            lambda self, val: object.__setattr__(self, "_non_interactive", bool(val)),
        ),
    )

ANSI_ESCAPE_REGEX = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi_codes(text: str) -> str:
    """Strip ANSI escape sequences from text string."""
    if not text:
        return text
    return ANSI_ESCAPE_REGEX.sub("", text)


class ANSIStrippingWriter:
    """Stream wrapper that strips ANSI escape sequences before writing."""

    def __init__(self, stream):
        """Initialize stream wrapper."""
        self.stream = stream

    def write(self, text):
        """Strip ANSI codes and write text to wrapped stream."""
        if text:
            self.stream.write(strip_ansi_codes(text))

    def flush(self):
        """Flush the wrapped stream if flush method is available."""
        if hasattr(self.stream, "flush"):
            self.stream.flush()

    def isatty(self):
        """Return isatty status of wrapped stream."""
        return getattr(self.stream, "isatty", lambda: False)()

    def __getattr__(self, attr):
        """Delegate missing stream attributes to wrapped stream."""
        return getattr(self.stream, attr)


def write_smoke_test_error(message, include_traceback=False):
    """Write smoke test diagnostic error message and traceback to file."""
    import logging
    import os
    import sys
    import tempfile
    import traceback
    from pathlib import Path

    from app.config import get_app_dir
    from app.log_filter import scrub_diagnostic_text

    logger = logging.getLogger("app.main")

    err_str = message
    if include_traceback:
        err_str += "\n" + traceback.format_exc()

    # Perform inline synchronous scrubbing of diagnostic error text prior to disk output
    err_str = scrub_diagnostic_text(err_str)

    # Define primary locations
    primary_paths = []
    primary_paths.append(("current working directory", Path("smoke_test_error.txt")))
    if is_packaged():
        exe_dir = os.path.dirname(sys.executable)
        if exe_dir:
            primary_paths.append(
                ("executable directory", Path(exe_dir) / "smoke_test_error.txt")
            )

    # Try writing to primary paths
    primary_success = False
    for desc, path in primary_paths:
        try:
            abs_path = path.resolve()
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(err_str)
            primary_success = True
            logger.info(
                f"Successfully wrote diagnostic report to primary location ({desc}): {abs_path}"
            )
        except Exception as e:
            logger.warning(
                f"Failed to write diagnostic report to primary location ({desc}) at {path}: {e}"
            )

    # Fallback writing sequence
    if not primary_success:
        fallback_success = False

        # 1. Try writing to user home configuration directory
        try:
            app_dir = get_app_dir()
            app_path = (app_dir / "smoke_test_error.txt").resolve()
            with open(app_path, "w", encoding="utf-8") as f:
                f.write(err_str)
            fallback_success = True
            logger.warning(
                f"Diagnostic log fallback write succeeded. Saved to: {app_path}"
            )
        except Exception as e:
            logger.warning(
                f"Failed to write fallback diagnostic report to user home configuration directory: {e}"
            )

        # 2. Try writing to system temporary directory with atomic mkstemp and strict 0o600 permissions
        if not fallback_success:
            try:
                sys_temp_dir = tempfile.gettempdir()
                fd, path_str = tempfile.mkstemp(
                    prefix="smoke_test_error_", suffix=".txt", dir=sys_temp_dir
                )
                abs_path = Path(path_str).resolve()
                try:
                    if hasattr(os, "fchmod"):
                        os.fchmod(fd, 0o600)
                    else:
                        os.chmod(abs_path, 0o600)
                except (OSError, AttributeError, TypeError):
                    try:
                        os.chmod(abs_path, 0o600)
                    except (OSError, AttributeError, TypeError):
                        pass

                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(err_str)

                fallback_success = True
                logger.warning(
                    f"Diagnostic log fallback write succeeded. Saved to: {abs_path}"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to write fallback diagnostic report to system temporary directory: {e}"
                )

        if not fallback_success:
            logger.error("All diagnostic log write options failed.")


def run_smoke_test():
    """Run a complete database smoke test to verify SQLCipher encryption and connectivity."""
    print("Starting automated database connection and encryption smoke test...")
    import tempfile

    # Create a temporary directory for testing to avoid side effects
    temp_dir = tempfile.mkdtemp()
    conn = None
    try:
        from app.core.db_conn import clear_connection_cache

        # Pre-flight check: try importing sqlcipher3 directly to log any specific DLL load failures
        try:
            from sqlcipher3 import dbapi2 as sqlite3_direct  # noqa: F401

            print("Direct sqlcipher3 import successful.")
        except Exception as import_err:
            import traceback  # noqa: F401

            write_smoke_test_error(
                f"Pre-flight import of sqlcipher3 failed with exception: {import_err}",
                include_traceback=True,
            )

        db_path = os.path.join(temp_dir, "smoke_test.db")
        print(f"Temporary database path: {db_path}")

        # Connect to the database using our actual connection function
        from app.core.db_conn import HAS_SQLCIPHER, get_db_connection

        if not HAS_SQLCIPHER:
            err_msg = "Error: SQLCipher driver is missing from runtime environment!"
            print(err_msg)
            write_smoke_test_error(err_msg, include_traceback=False)
            sys.exit(1)

        conn = get_db_connection(db_path)
        print("Successfully opened connection and verified SQLCipher driver.")

        # Create a test table, insert and read values
        with conn:
            cursor = conn.cursor()
            cursor.execute(
                "CREATE TABLE test_smoke (id INTEGER PRIMARY KEY, secret_val TEXT)"
            )
            cursor.execute(
                "INSERT INTO test_smoke (secret_val) VALUES (?)", ("SuperSecretData",)
            )

            cursor.execute("SELECT secret_val FROM test_smoke WHERE id = 1")
            row = cursor.fetchone()
            if not row or row[0] != "SuperSecretData":
                err_msg = "Error: Data validation failed inside the encrypted database!"
                print(err_msg)
                write_smoke_test_error(err_msg, include_traceback=False)
                sys.exit(1)

            # Double check cipher version via PRAGMA
            cursor.execute("PRAGMA cipher_version;")
            ver = cursor.fetchone()
            if not ver or not ver[0]:
                err_msg = (
                    "Error: PRAGMA cipher_version is empty! SQLCipher is not active."
                )
                print(err_msg)
                write_smoke_test_error(err_msg, include_traceback=False)
                sys.exit(1)
            print(f"Verified SQLCipher active version: {ver[0]}")

        print("Smoke test successfully completed. Encryption is active and verified!")
        sys.exit(0)
    except Exception as e:
        err_msg = f"Smoke test failed with exception: {e}"
        print(err_msg)
        write_smoke_test_error(err_msg, include_traceback=True)
        sys.exit(1)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            conn = None
        try:
            from app.core.db_conn import clear_connection_cache

            clear_connection_cache(only_current_and_inactive=False)
        except Exception:
            pass
        try:
            from app.core.resilient_file_ops import resilient_rmtree

            resilient_rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


def _extract_bool_arg(
    args: argparse.Namespace, attr: str, default: bool = False
) -> bool:
    """Read a boolean flag from the CLI args namespace, defaulting when absent."""
    return bool(getattr(args, attr, default))


def _persist_settings_sync(settings: AppSettings) -> None:
    """Flush pending debounced save timer and persist settings synchronously."""
    timer = getattr(settings, "_save_timer", None)
    if timer is not None:
        try:
            timer.cancel()
        except Exception:
            pass
    if hasattr(settings, "_save"):
        settings._save()


def apply_global_cli_flags(settings: AppSettings, args: argparse.Namespace) -> None:
    """Apply global AI consent, wizard-bypass, and non-interactive CLI/env flags."""
    accept_consent = _extract_bool_arg(args, "accept_ai_consent")
    decline_consent = _extract_bool_arg(args, "decline_ai_consent")
    skip_wizard = _extract_bool_arg(args, "skip_wizard")
    non_interactive_flag = _extract_bool_arg(args, "non_interactive")

    env_consent = os.environ.get("SORTIFY_AI_CONSENT")
    env_non_interactive = os.environ.get("NON_INTERACTIVE")

    # CLI consent flags persist to user settings and take precedence over env vars
    if accept_consent:
        settings.AI_CONSENT_GRANTED = True
        _persist_settings_sync(settings)
    elif decline_consent:
        settings.AI_CONSENT_GRANTED = False
        _persist_settings_sync(settings)
    elif env_consent is not None:
        env_consent_clean = env_consent.strip().lower()
        if env_consent_clean in ("1", "true", "yes", "on"):
            settings.override("AI_CONSENT_GRANTED", True)
        elif env_consent_clean in ("0", "false", "no", "off"):
            settings.override("AI_CONSENT_GRANTED", False)

    # Bypass TUI onboarding wizard when consent or skip flags are provided,
    # while only enabling non-interactive mode when explicitly requested.
    is_non_interactive_env = bool(
        env_non_interactive
        and env_non_interactive.strip().lower() in ("1", "true", "yes", "on")
    )
    if (
        skip_wizard
        or non_interactive_flag
        or is_non_interactive_env
        or accept_consent
        or decline_consent
    ):
        object.__setattr__(settings, "_skip_wizard", True)
        if settings.AI_CONSENT_GRANTED is None:
            settings.override("AI_CONSENT_GRANTED", False)

    if non_interactive_flag or is_non_interactive_env:
        object.__setattr__(settings, "_non_interactive", True)


def apply_config_overrides(settings: AppSettings, args: argparse.Namespace):
    """Apply command-line argument overrides and persist explicit CLI consent flags."""
    max_folders = getattr(args, "max_folders", None)
    if max_folders is not None:
        settings.override("MAX_FOLDERS", max_folders)

    strategy = getattr(args, "strategy", None)
    if strategy is not None:
        settings.override("SORTING_STRATEGY", strategy)

    conflict_policy = getattr(args, "conflict_policy", None)
    if conflict_policy is not None:
        settings.override("CONFLICT_POLICY", conflict_policy)

    contextual_renaming = getattr(args, "contextual_renaming", None)
    if contextual_renaming is not None:
        settings.override("CONTEXTUAL_RENAMING", contextual_renaming)

    duplicates = getattr(args, "duplicates", None)
    if duplicates is not None:
        settings.override("DUPLICATE_ACTION", duplicates)

    detect_original_by = getattr(args, "detect_original_by", None)
    if detect_original_by is not None:
        settings.override("DETECT_ORIGINAL_BY", detect_original_by)

    apply_global_cli_flags(settings, args)


def _maybe_provision_model_for_cli_consent(
    args: argparse.Namespace, settings: AppSettings
) -> None:
    """Initiate model verification/download with terminal progress on top-level --accept-ai-consent setup."""
    if not _extract_bool_arg(args, "accept_ai_consent"):
        return
    if getattr(args, "subcommand", None) is not None:
        return
    if getattr(args, "_cli_consent_model_checked", False):
        return
    object.__setattr__(args, "_cli_consent_model_checked", True)

    from app.core.downloader import provision_model_cli
    from app.core.exceptions import DownloaderError

    quiet = bool(getattr(args, "quiet", False))
    proxy = str(getattr(settings, "PROXY", "") or "").strip()
    try:
        provision_model_cli(proxy=proxy, quiet=quiet, stream=sys.stderr)
    except DownloaderError as exc:
        if not quiet:
            print(
                f"Warning: Could not download AI model weights ({exc}). "
                "Proceeding with local keyword/TF-IDF fallback. "
                "Retry anytime with 'smart-autosorter model download'.",
                file=sys.stderr,
            )
    except Exception as exc:
        if not quiet:
            print(
                f"Warning: Model provisioning check encountered an error ({exc}). "
                "Proceeding with local keyword/TF-IDF fallback.",
                file=sys.stderr,
            )


def _prompt_workspace_selection(args: argparse.Namespace) -> Path:
    """Prompt user for target workspace selection when CLI arguments are absent.

    Parameters
    ----------
    args : argparse.Namespace
        Command-line arguments namespace.

    Returns
    -------
    Path
        Resolved and verified target directory path.

    Raises
    ------
    ValueError
        If the selection is cancelled or input stream is closed.
    """
    quiet = bool(getattr(args, "quiet", False))
    no_color = bool(getattr(args, "no_color", False) or os.environ.get("NO_COLOR"))

    def _style(text: str, code: str) -> str:
        if no_color:
            return text
        return f"\033[{code}m{text}\033[0m"

    title = _style("Sortify - Workspace Onboarding", "1;36")
    prompt_str = _style("Select an option [1-5, q]: ", "1;32")

    if not quiet:
        print(f"\n{title}")
        print("No target directory specified. Please choose a workspace to proceed:\n")
    print("  [1] Demo Workspace         (sandbox/demo_workspace)")
    print(f"  [2] Downloads              ({Path.home() / 'Downloads'})")
    print(f"  [3] Documents              ({Path.home() / 'Documents'})")
    print("  [4] Native Folder Picker   (browse with file dialog)")
    print("  [5] Custom Directory       (enter path manually)")
    print("  [q] Quit\n")

    while True:
        try:
            choice = input(prompt_str).strip()
        except (KeyboardInterrupt, EOFError):
            raise ValueError("Directory selection cancelled.") from None

        choice_lower = choice.lower()
        if choice_lower in ("q", "quit", "exit"):
            raise ValueError("Directory selection cancelled.")

        if choice in ("1", "demo"):
            from app.demo import ensure_demo_workspace

            target, _seeded = ensure_demo_workspace("sandbox/demo_workspace")
            return target

        if choice in ("2", "downloads"):
            target = (Path.home() / "Downloads").resolve()
            if not target.exists() or not target.is_dir():
                print(
                    f"Error: Target directory '{target}' does not exist or is not a directory.",
                    file=sys.stderr,
                )
                continue
            return target

        if choice in ("3", "documents"):
            target = (Path.home() / "Documents").resolve()
            if not target.exists() or not target.is_dir():
                print(
                    f"Error: Target directory '{target}' does not exist or is not a directory.",
                    file=sys.stderr,
                )
                continue
            return target

        if choice in ("4", "browse", "native", "picker", "dialog"):
            try:
                from app.core.native_dialog import (
                    NativeDialogError,
                    select_native_directory,
                )

                selected = select_native_directory()
            except NativeDialogError as exc:
                print(
                    f"Native folder dialog unavailable: {exc}. Please enter path manually.",
                    file=sys.stderr,
                )
                continue
            except Exception as exc:
                print(
                    f"Error opening folder dialog: {exc}.",
                    file=sys.stderr,
                )
                continue

            if not selected:
                print("Directory selection cancelled.", file=sys.stderr)
                continue

            target = Path(selected).expanduser().resolve()
            if not target.exists() or not target.is_dir():
                print(
                    f"Error: Selected directory '{target}' does not exist or is not a directory.",
                    file=sys.stderr,
                )
                continue
            return target

        if choice in ("5", "custom", "path"):
            try:
                path_input = input("Enter directory path: ").strip()
            except (KeyboardInterrupt, EOFError):
                raise ValueError("Directory selection cancelled.") from None

            if not path_input:
                continue

            target = Path(path_input).expanduser().resolve()
            if not target.exists() or not target.is_dir():
                print(
                    f"Error: Target directory '{target}' does not exist or is not a directory.",
                    file=sys.stderr,
                )
                continue
            return target

        print(
            f"Invalid selection '{choice}'. Please choose 1-5 or 'q' to quit.",
            file=sys.stderr,
        )


def resolve_preset_and_directory(
    args: argparse.Namespace, settings: AppSettings | None = None
) -> Path:
    """Resolve preset choice or target directory path from CLI arguments."""
    preset = getattr(args, "preset", None)
    directory = getattr(args, "directory", None)

    if preset:
        preset_str = str(preset).lower()
        if preset_str == "demo":
            from app.demo import ensure_demo_workspace

            target, _seeded = ensure_demo_workspace("sandbox/demo_workspace")
            return target
        elif preset_str == "downloads":
            return (Path.home() / "Downloads").resolve()
        elif preset_str == "documents":
            return (Path.home() / "Documents").resolve()
        else:
            raise ValueError(
                f"Unknown preset choice '{preset}'. Choices are: demo, downloads, documents."
            )

    if directory:
        return Path(directory).resolve()

    # Determine non-interactive status via settings or fallback
    is_non_interactive = False
    if settings is not None:
        try:
            is_non_interactive = bool(settings.NON_INTERACTIVE)
        except AttributeError:
            is_non_interactive = bool(getattr(settings, "_non_interactive", False))
    else:
        is_non_interactive = bool(
            getattr(args, "non_interactive", False)
            or (
                os.environ.get("NON_INTERACTIVE", "").strip().lower()
                in ("1", "true", "yes", "on")
            )
        )

    if getattr(args, "non_interactive", False):
        is_non_interactive = True

    is_tty = bool(
        sys.stdin is not None and hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    )

    if not is_tty or is_non_interactive:
        raise ValueError("Either target directory or --preset must be specified.")

    target = _prompt_workspace_selection(args)
    args.directory = str(target)
    return target


def _print_plan_for_humans(
    plan: dict, target_path: Path, verb: str, quiet: bool = False
) -> None:
    """Print the plan tree to stdout and a one-line summary to stderr."""
    lines, n_files, n_folders = format_plan_tree(plan)
    plan_counts = count_plan_actions(plan)
    if not lines:
        print(f"Nothing to sort in '{target_path}'.", file=sys.stderr)
        if not quiet and plan_counts["skipped"] > 0:
            print(
                f"0 moved, {plan_counts['skipped']} skipped.",
                file=sys.stderr,
            )
        return
    if not quiet:
        dup_parts: list[str] = []
        if plan_counts["staged"] > 0:
            dup_parts.append(f"{plan_counts['staged']} staged")
        if plan_counts["trashed"] > 0:
            dup_parts.append(f"{plan_counts['trashed']} staged for trash")
        if plan_counts["skipped"] > 0:
            dup_parts.append(f"{plan_counts['skipped']} skipped")
        dup_note = f" ({', '.join(dup_parts)})" if dup_parts else ""
        print(
            f"{verb} {n_files} file(s) into {n_folders} folder(s){dup_note} in '{target_path}':",
            file=sys.stderr,
        )
    print("\n".join(lines))


_reroot_plan = reroot_plan


def _validate_headless_duplicate_mode(
    args: argparse.Namespace, settings: AppSettings
) -> None:
    """Reject ``--duplicates review`` (or configured ``review``) in headless CLI execution."""
    err_msg = validate_duplicate_mode_for_headless(
        getattr(settings, "DUPLICATE_ACTION", "off"),
        is_tui=False,
    )
    if err_msg:
        emit_cli_error(
            err_msg,
            json_mode=bool(getattr(args, "json", False)),
            exit_code=2,
        )


def _maybe_launch_tui(
    args: argparse.Namespace,
    settings: AppSettings,
    initial_dir: str | None = None,
    *,
    open_dropzone: bool = False,
    initial_drop_paths: list[str] | None = None,
    initial_dest_dir: str | None = None,
) -> None:
    """Launch the interactive TUI and exit when ``--tui`` or ``--interactive`` is set."""
    if getattr(args, "tui", False) or getattr(args, "interactive", False):
        err_msg = validate_duplicate_mode_for_headless(
            getattr(settings, "DUPLICATE_ACTION", "off"),
            is_tui=True,
            non_interactive=_extract_bool_arg(args, "non_interactive"),
        )
        if err_msg:
            emit_cli_error(
                err_msg,
                json_mode=bool(getattr(args, "json", False)),
                exit_code=2,
            )
        from app.ui.tui import run_tui

        tui_kwargs: dict[str, Any] = {
            "skip_wizard": _extract_bool_arg(args, "skip_wizard"),
            "non_interactive": _extract_bool_arg(args, "non_interactive"),
        }
        if open_dropzone:
            tui_kwargs["open_dropzone"] = True
        if initial_drop_paths is not None:
            tui_kwargs["initial_drop_paths"] = initial_drop_paths
        if initial_dest_dir is not None:
            tui_kwargs["initial_dest_dir"] = initial_dest_dir
        run_tui(
            settings,
            initial_dir,
            **tui_kwargs,
        )
        sys.exit(0)


def _prepare_cli_target_directory(
    args: argparse.Namespace,
    settings: AppSettings,
    *,
    warn_if_ml_unavailable: bool = False,
):
    """Apply CLI overrides once, optionally launch TUI, and validate the target folder."""
    apply_config_overrides(settings, args)

    if warn_if_ml_unavailable:
        from app.core.verifier import is_ml_available

        if not is_ml_available():
            requested_ml = (
                getattr(args, "strategy", None) == "generative"
                or getattr(settings, "SORTING_STRATEGY", None) == "generative"
                or getattr(settings, "AI_ASSISTED_NAMING", False)
            )
            if requested_ml:
                print(
                    "Warning: Machine learning features requested, but ML dependencies are not installed.\n"
                    "To enable ML features, please install the 'ml' extra group: uv sync --extra ml (or pip install '.[ml]')\n"
                    "Proceeding in offline fallback mode.",
                    file=sys.stderr,
                )

    _maybe_launch_tui(args, settings, getattr(args, "directory", None))
    _validate_headless_duplicate_mode(args, settings)

    is_json = getattr(args, "json", False)
    try:
        target_path = resolve_preset_and_directory(args, settings)
    except ValueError as err:
        emit_cli_error(
            f"Error: {err}",
            json_mode=is_json,
            exit_code=1,
        )

    if not target_path.exists() or not target_path.is_dir():
        emit_cli_error(
            f"Error: Target directory '{target_path}' does not exist or is not a directory.",
            json_mode=is_json,
            exit_code=1,
        )

    args.directory = str(target_path)
    return target_path


def _maybe_export_simulation_report(
    args: argparse.Namespace,
    plan: Any,
    base_dir: str,
) -> None:
    """Export a JSON or HTML simulation report when ``--export-report`` is provided."""
    export_report_path = getattr(args, "export_report", None)
    if not export_report_path:
        return

    from app.core.simulation_exporter import SimulationExporter

    fmt = getattr(args, "report_format", None)
    if not fmt:
        fmt = "json" if str(export_report_path).lower().endswith(".json") else "html"
    fmt = str(fmt).lower()

    exporter = SimulationExporter(plan, base_dir=base_dir)
    if fmt == "json":
        exporter.export_json(export_report_path)
    else:
        exporter.export_html(export_report_path)

    if not getattr(args, "quiet", False):
        print(
            f"Exported simulation report ({fmt.upper()}) to '{export_report_path}'.",
            file=sys.stderr,
        )


def handle_sort_command(args: argparse.Namespace, settings: AppSettings):
    """Execute document batch sorting or launch interactive TUI."""
    import json
    from pathlib import Path

    target_path = _prepare_cli_target_directory(
        args, settings, warn_if_ml_unavailable=True
    )

    session = None
    try:
        from app.core.pipeline import SortPipeline, reroot_plan
        from app.core.session import AppSession

        dest_dir = getattr(args, "dest_dir", None)
        dest_abs = str(Path(dest_dir).resolve()) if dest_dir else None
        dry_run = getattr(args, "dry_run", False)
        json_output = getattr(args, "json", False)

        session = AppSession(settings, base_dir=str(target_path))
        pipeline = SortPipeline(session)
        plan = pipeline.plan(dest_dir=dest_abs)

        if dest_dir and dest_abs:
            plan = reroot_plan(plan, dest_dir, source_dir=str(target_path))
            session.base_dir = dest_abs

        serializable_plan = _make_json_serializable(plan)
        _maybe_export_simulation_report(args, plan, base_dir=str(target_path))

        if dry_run:
            result = {
                "status": "success",
                "dry_run": True,
                "target_directory": str(target_path),
                "destination_directory": (
                    str(Path(dest_dir).resolve()) if dest_dir else str(target_path)
                ),
                "plan": serializable_plan,
            }
        else:
            flush_trash = bool(getattr(args, "flush_trash", False))
            outcome = pipeline.execute(plan, flush_trash=flush_trash)
            moved_count = len(outcome.moved) + len(outcome.quarantined)
            staged_count = len(outcome.staged)
            trashed_count = len(outcome.trashed)
            trash_handoff = (outcome.summary or {}).get("trash_handoff") or {}
            trash_errors = dict(trash_handoff.get("errors") or {})
            result = {
                "status": "success",
                "dry_run": False,
                "target_directory": str(target_path),
                "destination_directory": (
                    str(Path(dest_dir).resolve()) if dest_dir else str(target_path)
                ),
                "moved_count": moved_count,
                "staged_count": staged_count,
                "trashed_count": trashed_count,
                "staged": list(outcome.staged),
                "trashed": list(outcome.trashed),
                "plan": serializable_plan,
                "summary": outcome.summary,
            }
            if trash_errors:
                result["trash_errors"] = trash_errors

        quiet = getattr(args, "quiet", False)
        if json_output:
            sys.stdout.write(json.dumps(result, indent=2) + "\n")
            sys.stdout.flush()
        else:
            if dry_run:
                _print_plan_for_humans(
                    serializable_plan,
                    target_path,
                    "Would move",
                    quiet=quiet,
                )
                if not quiet:
                    print(
                        "Dry run: no files were moved. Run again without --dry-run "
                        "to move them, or add --json for machine-readable output.",
                        file=sys.stderr,
                    )
            else:
                reversible_count = moved_count + staged_count
                total_handled = moved_count + staged_count + trashed_count
                if total_handled == 0 and not outcome.failed:
                    print(
                        f"Nothing to sort in '{target_path}'.",
                        file=sys.stderr,
                    )
                    if not quiet and outcome.skipped:
                        print(outcome.describe(), file=sys.stderr)
                else:
                    lines, _n_files, n_folders = format_plan_tree(serializable_plan)
                    if lines:
                        if not quiet:
                            extra_counts: list[str] = []
                            if staged_count > 0:
                                extra_counts.append(f"{staged_count} staged")
                            if trashed_count > 0:
                                extra_counts.append(f"{trashed_count} trashed")
                            extra_note = (
                                f" ({', '.join(extra_counts)})" if extra_counts else ""
                            )
                            print(
                                f"Moved {moved_count} file(s) into {n_folders} folder(s){extra_note} in '{target_path}':",
                                file=sys.stderr,
                            )
                        print("\n".join(lines))
                    elif total_handled == 0:
                        print(
                            f"Nothing to sort in '{target_path}'.",
                            file=sys.stderr,
                        )
                    if not quiet:
                        print(outcome.describe(), file=sys.stderr)
                        for source, reason in outcome.failed:
                            print(f"  Failed: {source}: {reason}", file=sys.stderr)
                    for staged_p, err_msg in trash_errors.items():
                        print(
                            f"Warning: OS Trash handoff failed for '{staged_p}': {err_msg} (kept in reversible staging).",
                            file=sys.stderr,
                        )
                    if not quiet and reversible_count > 0 and outcome.session_id:
                        print(
                            "To undo this sort, run: smart-autosorter undo --latest",
                            file=sys.stderr,
                        )

        sys.exit(0)
    except Exception as e:
        emit_cli_error(
            f"Error during sorting operation: {e}",
            json_mode=getattr(args, "json", False),
            exit_code=1,
        )
    finally:
        if session is not None:
            session.close()


def handle_scan_command(args: argparse.Namespace, settings: AppSettings):
    """Execute directory scanning and sorting analysis or launch interactive TUI."""
    import json

    target_path = _prepare_cli_target_directory(args, settings)

    session = None
    try:
        from app.core.pipeline import SortPipeline
        from app.core.scanner import get_files_recursively
        from app.core.session import AppSession

        session = AppSession(settings, base_dir=str(target_path))
        files = get_files_recursively(str(target_path), settings=settings)
        plan = SortPipeline(session).plan(files)
        serializable_plan = _make_json_serializable(plan)

        _maybe_export_simulation_report(args, plan, base_dir=str(target_path))

        result = {
            "status": "success",
            "target_directory": str(target_path),
            "files_scanned": len(files),
            "plan": serializable_plan,
        }

        quiet = getattr(args, "quiet", False)
        if getattr(args, "json", False):
            sys.stdout.write(json.dumps(result, indent=2) + "\n")
            sys.stdout.flush()
        else:
            if not quiet:
                print(
                    f"Scanned {len(files)} file(s) in '{target_path}'. No files were moved.",
                    file=sys.stderr,
                )
            _print_plan_for_humans(
                serializable_plan, target_path, "Would move", quiet=quiet
            )

        sys.exit(0)
    except Exception as e:
        emit_cli_error(
            f"Error during scan operation: {e}",
            json_mode=getattr(args, "json", False),
            exit_code=1,
        )
    finally:
        if session is not None:
            session.close()


def _parse_config_cli_value(key_upper: str, raw_value: str, curr_val: Any) -> Any:
    """Parse a raw CLI string value for `config --set` based on field schema metadata."""
    import json
    import types
    import typing

    from app.config import Settings

    field_info = Settings.model_fields.get(key_upper)
    annotation = field_info.annotation if field_info is not None else type(curr_val)
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    is_optional_bool = origin in (typing.Union, types.UnionType) and (
        bool in args and type(None) in args
    )

    if is_optional_bool:
        val_lower = raw_value.strip().lower()
        if val_lower in ("null", "none"):
            return None
        if val_lower in ("true", "1", "yes", "on"):
            return True
        if val_lower in ("false", "0", "no", "off"):
            return False
        raise ValueError(
            f"Invalid optional boolean value '{raw_value}'. Use true, false, or null."
        )

    if annotation is bool or isinstance(curr_val, bool):
        val_lower = raw_value.strip().lower()
        if val_lower in ("true", "1", "yes", "on"):
            return True
        if val_lower in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"Invalid boolean value '{raw_value}'. Use true or false.")

    if origin in (set, list) or isinstance(curr_val, (set, list)):
        target_is_set = origin is set or isinstance(curr_val, set)
        stripped = raw_value.strip()
        if stripped.startswith("["):
            parsed = json.loads(stripped)
            if not isinstance(parsed, list):
                raise ValueError("JSON collection input must be a JSON array.")
            return set(parsed) if target_is_set else parsed
        items = [item.strip() for item in raw_value.split(",") if item.strip()]
        return set(items) if target_is_set else items

    if origin is dict or annotation is dict or isinstance(curr_val, dict):
        parsed = json.loads(raw_value.strip())
        if not isinstance(parsed, dict):
            raise ValueError("Mapping configuration input must be a JSON object.")
        return parsed

    if annotation is int or (
        isinstance(curr_val, int) and not isinstance(curr_val, bool)
    ):
        return int(raw_value)

    if annotation is float or isinstance(curr_val, float):
        return float(raw_value)

    return raw_value


def handle_config_command(args: argparse.Namespace, settings: AppSettings):
    """View or update application settings."""
    import json

    apply_global_cli_flags(settings, args)
    json_output = bool(getattr(args, "json", False))

    if getattr(args, "set", None):
        for key, value in args.set:
            key_upper = key.upper()
            if hasattr(settings._settings_model, key_upper):
                curr_val = getattr(settings, key_upper)
                try:
                    new_val = _parse_config_cli_value(key_upper, value, curr_val)
                    setattr(settings, key_upper, new_val)
                    settings.flush()
                except Exception as ex:
                    emit_cli_error(
                        f"Error: Failed to set configuration key '{key_upper}' to '{value}': {ex}",
                        json_mode=json_output,
                        exit_code=1,
                    )
            else:
                emit_cli_error(
                    f"Error: Unknown configuration key '{key}'.",
                    json_mode=json_output,
                    exit_code=1,
                )

    quiet = getattr(args, "quiet", False)
    settings_dict = settings._settings_model.model_dump(mode="json")
    if json_output:
        sys.stdout.write(json.dumps(settings_dict, indent=2) + "\n")
        sys.stdout.flush()
    else:
        if not quiet:
            print("Application Settings:", file=sys.stderr)
        for k, v in settings_dict.items():
            print(f"  {k}: {v}")

    sys.exit(0)


def handle_daemon_command(args: argparse.Namespace, settings: AppSettings):
    """Launch or send control commands to persistent directory-watching daemon."""
    import asyncio
    import json
    import time
    from pathlib import Path

    from app.core.daemon import start_daemon
    from app.core.ipc import DaemonIPCClient

    apply_config_overrides(settings, args)
    json_output = bool(getattr(args, "json", False))

    known_actions = {"status", "health", "metrics", "pause", "resume", "stop", "start"}

    first_arg = getattr(args, "action", None)
    second_arg = getattr(args, "directory", None)

    if first_arg in known_actions:
        action = first_arg
        target_dir = second_arg or os.getcwd()
    elif first_arg is not None:
        action = "start"
        target_dir = first_arg
    else:
        action = "start"
        target_dir = second_arg or os.getcwd()

    target_path = Path(target_dir).resolve()

    if action == "start":
        _validate_headless_duplicate_mode(args, settings)
        if not target_path.exists() or not target_path.is_dir():
            emit_cli_error(
                f"Error: Target directory '{target_dir}' does not exist or is not a directory.",
                json_mode=json_output,
                exit_code=1,
            )
        start_daemon(settings, str(target_path))
    else:
        client = DaemonIPCClient(str(target_path))
        is_watch = getattr(args, "watch", False)

        if action == "status" and is_watch:
            if not json_output:
                print(
                    f"Monitoring daemon status telemetry for '{target_path}' (Press Ctrl+C to exit)..."
                )
            try:
                while True:
                    try:
                        res = asyncio.run(client.send_command("status"))
                        if json_output:
                            sys.stdout.write(
                                json.dumps(_make_json_serializable(res)) + "\n"
                            )
                            sys.stdout.flush()
                        else:
                            status_str = str(res.get("status", "unknown")).upper()
                            pid = res.get("pid", "unknown")
                            qd = res.get("queue_depth", 0)
                            max_cap = res.get("max_queue_capacity", 1000)
                            workers = res.get("active_workers", 0)
                            uptime = res.get("uptime_seconds", 0)
                            triages = res.get("active_triage_paths", [])
                            print(
                                f"[{time.strftime('%H:%M:%S')}] Status: {status_str} (PID {pid}) | Queue: {qd}/{max_cap} | Workers: {workers} | Uptime: {uptime}s | Active Triage: {len(triages)}"
                            )
                    except Exception as err:
                        print(f"Error querying daemon status: {err}", file=sys.stderr)
                    time.sleep(1.0)
            except KeyboardInterrupt:
                if not json_output:
                    print("\nStopped watch monitoring.")
                sys.exit(0)

        try:
            res = asyncio.run(client.send_command(action))
            if json_output:
                sys.stdout.write(
                    json.dumps(_make_json_serializable(res), indent=2) + "\n"
                )
                sys.stdout.flush()
                return
            if action == "status":
                print(f"Daemon Status (PID: {res.get('pid')}):")
                print(f"  Status: {str(res.get('status', '')).upper()}")
                print(f"  Base Directory: {res.get('base_dir')}")
                print(f"  Uptime: {res.get('uptime_seconds')}s")
                print(
                    f"  Queue Depth: {res.get('queue_depth')} / {res.get('max_queue_capacity')}"
                )
                print(f"  Active Workers: {res.get('active_workers')}")
                print(f"  Relocating Files: {res.get('is_moving')}")
                print(
                    f"  Active Triage Paths: {res.get('active_triage_paths') or 'None'}"
                )
            elif action == "health":
                print("Daemon Health Check:")
                print(f"  Health Status: {str(res.get('status', '')).upper()}")
                print(f"  PID: {res.get('pid')}")
                print(f"  Queue Depth: {res.get('queue_depth')}")
                print(f"  Queue Full: {res.get('queue_full')}")
                print(f"  Uptime: {res.get('uptime_seconds')}s")
            elif action == "metrics":
                print("Daemon Metrics:")
                print(f"  Events Enqueued: {res.get('events_enqueued')}")
                print(f"  Events Processed: {res.get('events_processed')}")
                print(f"  Queue Depth: {res.get('queue_depth')}")
                print(f"  Active Workers: {res.get('active_workers')}")
                print(f"  Uptime: {res.get('uptime_seconds')}s")
            elif action == "pause":
                print(f"Daemon paused successfully ({res.get('message')}).")
            elif action == "resume":
                print(f"Daemon resumed successfully ({res.get('message')}).")
            elif action == "stop":
                print(f"Daemon shutdown initiated ({res.get('message')}).")
            else:
                print(json.dumps(_make_json_serializable(res), indent=2))
        except Exception as e:
            emit_cli_error(
                str(e),
                json_mode=json_output,
                exit_code=1,
            )


def handle_undo_command(args: argparse.Namespace, settings: AppSettings):
    """Execute undo subcommand for session rollback or listing history records."""
    from app.cli.undo_cli import execute_undo_command

    execute_undo_command(
        args,
        settings,
        apply_overrides_fn=apply_global_cli_flags,
        list_sessions_fn=list_sessions,
    )


def handle_drop_command(args: argparse.Namespace, settings: AppSettings):
    """Execute drop triage on specified target files or directories or launch TUI dropzone."""
    import json

    apply_config_overrides(settings, args)
    json_output = getattr(args, "json", False)

    raw_p = getattr(args, "paths", None)
    dest_arg = getattr(args, "dest_dir", None)
    dir_arg = getattr(args, "directory", None)
    resolved_dest_arg: str | None = (
        str(Path(dest_arg).expanduser().resolve())
        if dest_arg and str(dest_arg).strip()
        else None
    )
    init_dir: str | None = None
    if resolved_dest_arg:
        init_dir = resolved_dest_arg
    elif dir_arg:
        abs_dir = os.path.abspath(os.path.expanduser(str(dir_arg)))
        init_dir = abs_dir if os.path.isdir(abs_dir) else os.path.dirname(abs_dir)
    elif raw_p:
        first_clean = str(raw_p[0]).strip().strip("'\"")
        if first_clean:
            abs_first = os.path.abspath(os.path.expanduser(first_clean))
            init_dir = (
                abs_first if os.path.isdir(abs_first) else os.path.dirname(abs_first)
            )

    _maybe_launch_tui(
        args,
        settings,
        init_dir,
        open_dropzone=True,
        initial_drop_paths=list(raw_p) if raw_p else [],
        initial_dest_dir=resolved_dest_arg,
    )
    _validate_headless_duplicate_mode(args, settings)

    if getattr(args, "preset", None):
        print(
            "Warning: --preset has no effect with drop; it sorts the paths you give it.",
            file=sys.stderr,
        )

    raw_paths = getattr(args, "paths", [])
    if not raw_paths:
        emit_cli_error(
            "Error: No target paths provided for drop command.",
            json_mode=json_output,
            exit_code=1,
        )

    from app.core.pipeline import DropBatchCoordinator

    coordinator = DropBatchCoordinator(settings)
    dest_dir = getattr(args, "dest_dir", None)
    validated_paths, issues, resolved_dest, dest_err = coordinator.validate(
        raw_paths, dest_dir=dest_dir
    )
    missing_issue = next((i for i in issues if i.reason == "missing"), None)
    if missing_issue is not None:
        emit_cli_error(
            f"Error: Target path '{missing_issue.clean_path}' does not exist.",
            json_mode=json_output,
            exit_code=1,
        )
    protected_issue = next((i for i in issues if i.reason == "protected"), None)
    if protected_issue is not None:
        emit_cli_error(
            f"Error: Target path '{protected_issue.clean_path}' is protected.",
            json_mode=json_output,
            exit_code=1,
        )

    if not validated_paths:
        blocked_detail = next(
            (
                i
                for i in issues
                if i.detail in ("app_bundle", "quarantine", "ignored_extension")
            ),
            None,
        )
        reason_msg = "Error: No valid target paths to process."
        if blocked_detail is not None:
            if blocked_detail.detail == "app_bundle":
                reason_msg = f"Error: Application bundle '{blocked_detail.clean_path}' cannot be sorted."
            elif blocked_detail.detail == "quarantine":
                reason_msg = f"Error: Quarantine folder '{blocked_detail.clean_path}' cannot be sorted."
            elif blocked_detail.detail == "ignored_extension":
                reason_msg = f"Error: Ignored file extension '{blocked_detail.clean_path}' cannot be sorted."
        emit_cli_error(
            reason_msg,
            json_mode=json_output,
            exit_code=1,
        )

    if dest_err == "protected":
        emit_cli_error(
            f"Error: Destination path '{dest_dir}' is protected.",
            json_mode=json_output,
            exit_code=1,
        )
    if dest_err == "not_directory":
        emit_cli_error(
            f"Error: Destination path '{dest_dir}' is not a directory.",
            json_mode=json_output,
            exit_code=1,
        )

    dry_run = getattr(args, "dry_run", False)
    flush_trash = bool(getattr(args, "flush_trash", False))
    quiet = getattr(args, "quiet", False)

    batch_plan = coordinator.plan_batch(validated_paths, dest_dir=resolved_dest)
    batch_result = coordinator.execute_batch(
        batch_plan, dry_run=dry_run, flush_trash=flush_trash
    )
    processed_results = batch_result.target_results
    drop_trash_errors = dict((batch_result.trash_handoff or {}).get("errors") or {})

    result: dict[str, Any] = {
        "status": "success",
        "command": "drop",
        "dry_run": dry_run,
        "session_id": batch_result.session_id,
        "destination_dir": batch_result.resolved_dest,
        "processed_count": len(processed_results),
        "moved_count": batch_result.moved_count,
        "staged_count": batch_result.staged_count,
        "trashed_count": batch_result.trashed_count,
        "results": processed_results,
    }
    if batch_result.trash_handoff:
        result["trash_handoff"] = batch_result.trash_handoff
    if drop_trash_errors:
        result["trash_errors"] = drop_trash_errors

    if json_output:
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
        sys.stdout.flush()
    else:
        if not quiet:
            count_parts = [f"{batch_result.moved_count} moved"]
            if batch_result.staged_count > 0:
                count_parts.append(f"{batch_result.staged_count} staged")
            if batch_result.trashed_count > 0:
                count_parts.append(f"{batch_result.trashed_count} trashed")
            print(
                f"Drop triage completed successfully for {len(processed_results)} target item(s) ({', '.join(count_parts)}).",
                file=sys.stderr,
            )
        for staged_p, err_msg in drop_trash_errors.items():
            print(
                f"Warning: OS Trash handoff failed for '{staged_p}': {err_msg} (kept in reversible staging).",
                file=sys.stderr,
            )
        print(json.dumps(_make_json_serializable(result), indent=2))

    sys.exit(0)


_CLI_EXAMPLES = """\
examples:
  smart-autosorter                         open the full-screen TUI
  smart-autosorter sort ~/Downloads --dry-run
                                           preview the plan without moving files
  smart-autosorter sort ~/Downloads        sort the folder
  smart-autosorter undo --latest           move the last sort's files back
  smart-autosorter undo --list             list past sorts you can undo

Run 'smart-autosorter <command> --help' for a command's options.
"""


class SortifyArgumentParser(argparse.ArgumentParser):
    """Argument parser that emits structured JSON errors when --json is requested."""

    _active_cli_args: Any = None

    @staticmethod
    def _normalize_dash_option_values(raw_args: list[str]) -> list[str]:
        """Allow '--detect-original-by -created' alongside '--detect-original-by=-created'."""
        normalized: list[str] = []
        idx = 0
        while idx < len(raw_args):
            token = raw_args[idx]
            if token == "--":
                normalized.extend(raw_args[idx:])
                break
            if (
                token == "--detect-original-by"
                and idx + 1 < len(raw_args)
                and raw_args[idx + 1].startswith("-")
                and not raw_args[idx + 1].startswith("--")
            ):
                normalized.append(f"{token}={raw_args[idx + 1]}")
                idx += 2
                continue
            normalized.append(token)
            idx += 1
        return normalized

    def parse_known_args(
        self,
        args: Any = None,
        namespace: Any = None,
    ) -> tuple[Any, list[str]]:
        """Normalize dash-prefixed option values and validate cross-level flag rules."""
        raw_list = list(sys.argv[1:]) if args is None else list(args)
        normalized = self._normalize_dash_option_values(raw_list)
        if SortifyArgumentParser._active_cli_args is None:
            SortifyArgumentParser._active_cli_args = normalized
        ns, remaining = super().parse_known_args(args=normalized, namespace=namespace)
        validate_cross_level_cli_flags(self, ns)
        return ns, remaining

    def parse_args(
        self,
        args: Any = None,
        namespace: Any = None,
    ) -> Any:
        """Parse CLI arguments and record active arguments for error handling."""
        raw_list = list(sys.argv[1:]) if args is None else list(args)
        normalized = self._normalize_dash_option_values(raw_list)
        SortifyArgumentParser._active_cli_args = normalized
        return super().parse_args(args=normalized, namespace=namespace)

    def error(self, message: str) -> NoReturn:
        """Emit structured JSON error or standard argparse text error.

        Parameters
        ----------
        message : str
            The parser error message describing the syntax or option error.
        """
        active_args = SortifyArgumentParser._active_cli_args
        has_json = (active_args is not None and "--json" in active_args) or (
            "--json" in sys.argv
        )
        if has_json:
            emit_cli_error(message, json_mode=True, exit_code=2)
        super().error(message)


def build_parser(prog: str | None = "smart-autosorter") -> argparse.ArgumentParser:
    """Build and return the main command-line argument parser for Smart AutoSorter AI Pro."""
    parser = SortifyArgumentParser(
        prog=prog,
        description=(
            "Smart AutoSorter AI Pro: sort a folder of documents into topic\n"
            "folders, offline. Every move can be undone."
        ),
        epilog=_CLI_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        dest="quiet",
        default=False,
        help="Suppress informational prints and non-essential progress output",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        dest="no_color",
        default=False,
        help="Disable ANSI color and style formatting",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        default=False,
        help="Run interactive CLI demo mode",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        default=False,
        help="Run automated database smoke test and exit",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output in structured JSON format",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        default=False,
        help="Launch the persistent directory-watching daemon",
    )
    parser.add_argument(
        "--debug-layout",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,  # developer tool: outline UI elements
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        default=False,
        help="Launch full-screen Textual TUI interface",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="Same as --tui",
    )
    ai_consent_group = parser.add_mutually_exclusive_group()
    ai_consent_group.add_argument(
        "--accept-ai-consent",
        action="store_true",
        default=False,
        help="Pre-configure AI consent as granted and bypass onboarding wizard",
    )
    ai_consent_group.add_argument(
        "--decline-ai-consent",
        action="store_true",
        default=False,
        help="Pre-configure AI consent as declined and bypass onboarding wizard",
    )
    parser.add_argument(
        "--skip-wizard",
        action="store_true",
        default=False,
        help="Bypass onboarding wizard modal during startup",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        default=False,
        help="Run in non-interactive mode and bypass interactive modal dialogs",
    )
    add_settings_source_args(parser, suppress_default=False)
    parser.add_argument(
        "--duplicates",
        type=str,
        choices=["off", "review", "skip", "stage", "trash"],
        default=None,
        help="Byte-identical duplicate handling mode (off, review, skip, stage, trash; review requires --tui or --interactive)",
    )
    parser.add_argument(
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
        default=None,
        help="Criterion used to select the surviving original in duplicate clusters",
    )

    subparsers: argparse._SubParsersAction = parser.add_subparsers(
        dest="subcommand", help="Available subcommands"
    )

    # Subcommand: sort
    parser_sort = subparsers.add_parser(
        "sort",
        parents=[build_global_subparser_parent(), build_sort_override_parent()],
        help="Run document sorting in headless batch processing mode",
    )
    parser_sort.add_argument(
        "directory", nargs="?", default=None, type=str, help="Target directory to sort"
    )
    parser_sort.add_argument(
        "--preset",
        type=str,
        choices=["demo", "downloads", "documents"],
        default=None,
        help="Use standard workspace preset directory (demo, downloads, documents)",
    )
    parser_sort.add_argument(
        "--dest-dir",
        type=str,
        default=None,
        help="Destination directory for sorted files",
    )
    parser_sort.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform dry run analysis without executing physical moves",
    )
    parser_sort.add_argument(
        "--flush-trash",
        action="store_true",
        default=False,
        help="Export staged duplicate copies to the OS Trash after move verification",
    )

    # Subcommand: scan
    parser_scan = subparsers.add_parser(
        "scan",
        parents=[build_global_subparser_parent(), build_sort_override_parent()],
        help="Run directory scanning and analysis without moving files",
    )
    parser_scan.add_argument(
        "directory", nargs="?", default=None, type=str, help="Target directory to scan"
    )
    parser_scan.add_argument(
        "--preset",
        type=str,
        choices=["demo", "downloads", "documents"],
        default=None,
        help="Use standard workspace preset directory (demo, downloads, documents)",
    )

    # Subcommand: config (global flags only; rejects transient sort overrides)
    parser_config = subparsers.add_parser(
        "config",
        parents=[build_global_subparser_parent()],
        help="View or update application configuration settings",
    )
    parser_config.add_argument(
        "--show",
        action="store_true",
        help="Display current configuration settings (the default)",
    )
    parser_config.add_argument(
        "--set",
        nargs=2,
        action="append",
        metavar=("KEY", "VALUE"),
        help="Set configuration KEY to VALUE",
    )

    # Subcommand: daemon
    parser_daemon = subparsers.add_parser(
        "daemon",
        parents=[build_global_subparser_parent(), build_sort_override_parent()],
        help="Launch or control persistent directory-watching daemon",
    )
    parser_daemon.add_argument(
        "action",
        nargs="?",
        default=None,
        help="Action (start, status, health, metrics, pause, resume, stop) or directory to watch",
    )
    parser_daemon.add_argument(
        "directory",
        nargs="?",
        default=None,
        help="Directory to watch or control",
    )
    parser_daemon.add_argument(
        "--watch",
        action="store_true",
        help="Continuously monitor status telemetry in real time",
    )

    # Subcommand: undo (consolidated in app.cli.undo_cli; global flags only)
    from app.cli.undo_cli import register_subparser as register_undo_subparser

    register_undo_subparser(subparsers)

    # Subcommand: drop
    parser_drop = subparsers.add_parser(
        "drop",
        parents=[build_global_subparser_parent(), build_sort_override_parent()],
        help="Quick file drop triage receiver for target files or directories",
    )
    parser_drop.add_argument(
        "paths",
        nargs="+",
        type=str,
        help="Target file or directory paths to process",
    )
    parser_drop.add_argument(
        "--preset",
        type=str,
        choices=["demo", "downloads", "documents"],
        default=None,
        # drop sorts the paths it is given; --preset is accepted only so
        # existing scripts don't break, and it warns when used.
        help=argparse.SUPPRESS,
    )
    parser_drop.add_argument(
        "--dest-dir",
        type=str,
        default=None,
        help="Destination directory for sorted files",
    )
    parser_drop.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform dry run analysis without executing physical moves",
    )
    parser_drop.add_argument(
        "--flush-trash",
        action="store_true",
        default=False,
        help="Export staged duplicate copies to the OS Trash after move verification",
    )

    # Register modular domain subcommands
    from app.cli import register_cli_subcommands

    register_cli_subcommands(subparsers)

    return parser


def _main_impl():
    """Execute the internal application GUI, CLI, or Demo routing logic."""
    import multiprocessing
    import sys

    prepare_frozen_windows_runtime()
    multiprocessing.freeze_support()

    parser = build_parser()

    known_subcommands = (
        "sort",
        "scan",
        "config",
        "daemon",
        "ledger",
        "model",
        "quarantine",
        "rules",
        "undo",
        "drop",
    )
    # `smart-autosorter [flags] DIR`: a directory with no subcommand. Skip
    # values belonging to top-level options so flag arguments are not mistaken
    # for the legacy positional directory.
    top_level_value_flags = {
        "--settings-file",
        "--profile",
        "--duplicates",
        "--detect-original-by",
    }
    legacy_directory = None
    skip_next = False
    for index, arg in enumerate(sys.argv[1:], start=1):
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            break
        if arg in top_level_value_flags:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        if arg not in known_subcommands:
            legacy_directory = sys.argv.pop(index)
        break

    args = parser.parse_args()
    if legacy_directory and not getattr(args, "directory", None):
        args.directory = legacy_directory
    if not hasattr(args, "directory"):
        args.directory = None

    if not hasattr(args, "quiet"):
        args.quiet = False
    if not hasattr(args, "no_color"):
        args.no_color = False

    no_color = getattr(args, "no_color", False) or bool(os.environ.get("NO_COLOR"))
    if no_color:
        if sys.stdout is not None:
            sys.stdout = ANSIStrippingWriter(sys.stdout)
        if sys.stderr is not None:
            sys.stderr = ANSIStrippingWriter(sys.stderr)

    if getattr(args, "smoke_test", False) is True:
        run_smoke_test()

    added_handlers: list[logging.Handler] = []
    settings = _configure_settings_and_logging(args, tracked_handlers=added_handlers)
    try:
        _run_configured_main(args, parser, settings)
    finally:
        _close_tracked_log_handlers(added_handlers)


def _close_tracked_log_handlers(handlers: list[logging.Handler]) -> None:
    """Flush, detach, and close log handlers added for the current CLI run."""
    root_logger = logging.getLogger()
    for handler in handlers:
        try:
            handler.flush()
        except Exception:
            pass
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass


def _run_configured_main(args: Any, parser: Any, settings: AppSettings) -> None:
    """Run configured CLI subcommands, headless modes, daemon, or interactive TUI."""
    _maybe_provision_model_for_cli_consent(args, settings)

    # Direct subcommand execution
    if getattr(args, "subcommand", None) == "sort":
        handle_sort_command(args, settings)
    elif getattr(args, "subcommand", None) == "scan":
        handle_scan_command(args, settings)
    elif getattr(args, "subcommand", None) == "config":
        handle_config_command(args, settings)
    elif getattr(args, "subcommand", None) == "daemon":
        handle_daemon_command(args, settings)
        return
    elif getattr(args, "subcommand", None) == "undo":
        handle_undo_command(args, settings)
    elif getattr(args, "subcommand", None) == "drop":
        handle_drop_command(args, settings)
    else:
        apply_global_cli_flags(settings, args)
        from app.cli import handle_cli_command

        if handle_cli_command(args, settings):
            return

    has_explicit_consent_flag = _extract_bool_arg(
        args, "accept_ai_consent"
    ) or _extract_bool_arg(args, "decline_ai_consent")
    is_non_interactive_mode = bool(
        getattr(settings, "_non_interactive", False)
        or _extract_bool_arg(args, "non_interactive")
        or (
            os.environ.get("NON_INTERACTIVE", "").strip().lower()
            in ("1", "true", "yes", "on")
        )
    )

    # Explicit Headless Guard for non-interactive streams without subcommands
    is_interactive = sys.stdin is not None and sys.stdin.isatty()
    is_explicit_ui = (
        getattr(args, "tui", False)
        or getattr(args, "interactive", False)
        or getattr(args, "demo", False)
        or getattr(args, "daemon", False)
    )

    if (not is_interactive or is_non_interactive_mode) and not is_explicit_ui:
        if getattr(args, "directory", None):
            # A bare directory opens the TUI, where the user confirms the plan.
            # With no terminal (cron, pipes, CI) nobody can confirm, so only
            # preview; moving files requires the explicit `sort` subcommand.
            args.dry_run = True
            if not getattr(args, "quiet", False):
                print(
                    "No interactive terminal detected: previewing the plan only. "
                    f"Run 'sort {args.directory}' to move files.",
                    file=sys.stderr,
                )
            handle_sort_command(args, settings)
        elif has_explicit_consent_flag:
            is_json = bool(getattr(args, "json", False) or "--json" in sys.argv)
            consent_state = bool(getattr(settings, "AI_CONSENT_GRANTED", False))
            if is_json:
                print(
                    json.dumps(
                        {
                            "status": "success",
                            "ai_consent_granted": consent_state,
                        },
                        indent=2,
                    )
                )
            elif not getattr(args, "quiet", False):
                state_label = "granted" if consent_state else "declined"
                print(
                    f"Saved AI consent preference: {state_label}.",
                    file=sys.stderr,
                )
            return
        else:
            if getattr(args, "json", False) or "--json" in sys.argv:
                emit_cli_error(
                    "Error: No target directory or subcommand specified.",
                    json_mode=True,
                    exit_code=2,
                )
            parser.print_help(sys.stderr)
            sys.exit(2)

    if (
        has_explicit_consent_flag
        and is_interactive
        and not is_non_interactive_mode
        and not getattr(args, "directory", None)
        and not getattr(args, "preset", None)
        and not getattr(args, "demo", False)
        and not getattr(args, "daemon", False)
    ):
        try:
            target_path = resolve_preset_and_directory(args, settings)
            args.directory = str(target_path)
        except ValueError as err:
            emit_cli_error(
                f"Error: {err}",
                json_mode=bool(getattr(args, "json", False)),
                exit_code=1,
            )

    # Verify embedded model integrity upfront if packaged / sandboxed
    if is_packaged():
        print("Verifying integrity of embedded model weights...", file=sys.stderr)
        try:
            from app.core.verifier import check_ai_status

            check_ai_status(settings)
            print("Model weights integrity verified successfully.", file=sys.stderr)
        except Exception as e:
            print(f"Startup verification failed: {e}", file=sys.stderr)
            write_smoke_test_error(
                f"Startup verification failed: {e}", include_traceback=True
            )
            sys.exit(1)

    # Automated headless startup reconciliation scan for incomplete file relocations
    try:
        from app.core.moves import MoveLedger
        from app.core.store import open_store

        ledger = MoveLedger(str(open_store().path), auto_init=True)
        ledger.reconcile_incomplete_transactions()
    except Exception as exc:
        logging.warning(
            f"Headless transaction ledger reconciliation on startup failed: {exc}"
        )

    if getattr(args, "daemon", False) is True:
        _validate_headless_duplicate_mode(args, settings)
        from app.core.daemon import start_daemon

        start_daemon(settings, args.directory)
    elif args.demo:
        apply_config_overrides(settings, args)
        from app.demo import run_demo

        run_demo(settings, args)
    else:
        from app.ui.tui import run_tui

        run_tui(
            settings,
            args.directory,
            skip_wizard=_extract_bool_arg(args, "skip_wizard"),
            non_interactive=_extract_bool_arg(args, "non_interactive"),
        )


def _configure_settings_and_logging(
    args: Any,
    tracked_handlers: list[logging.Handler] | None = None,
) -> AppSettings:
    """Load AppSettings while buffering early warnings into the configured log file."""

    class _StartupLogBuffer(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.INFO)
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    raw_sf = getattr(args, "settings_file", None)
    raw_prof = getattr(args, "profile", None)
    settings_filepath: str | None = None
    if raw_sf is not None or raw_prof is not None:
        from app.config import resolve_cli_settings_filepath

        try:
            settings_filepath = resolve_cli_settings_filepath(
                settings_file=raw_sf,
                profile=raw_prof,
            )
        except ValueError as err:
            is_json = bool(getattr(args, "json", False) or "--json" in sys.argv)
            emit_cli_error(f"Error: {err}", json_mode=is_json, exit_code=2)

    root_logger = logging.getLogger()
    pre_existing_handlers = set(root_logger.handlers)
    prev_root_level = root_logger.level
    startup_buffer = _StartupLogBuffer()
    root_logger.addHandler(startup_buffer)
    if not pre_existing_handlers and (
        prev_root_level == logging.NOTSET or prev_root_level > logging.INFO
    ):
        root_logger.setLevel(logging.INFO)
    try:
        settings = (
            AppSettings(filepath=settings_filepath)
            if settings_filepath
            else AppSettings()
        )
        if should_apply_sort_overrides(args):
            apply_config_overrides(settings, args)
        else:
            apply_global_cli_flags(settings, args)
    finally:
        root_logger.removeHandler(startup_buffer)
        if not pre_existing_handlers:
            root_logger.setLevel(prev_root_level)

    if settings.LOG_FILE:
        log_path = Path(settings.LOG_FILE)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    log_format = "%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s"
    logging.basicConfig(
        filename=settings.LOG_FILE,
        level=logging.INFO,
        format=log_format,
    )

    norm_log_file = os.path.normcase(os.path.abspath(settings.LOG_FILE))
    has_target_file_handler = any(
        isinstance(h, logging.FileHandler)
        and os.path.normcase(os.path.abspath(getattr(h, "baseFilename", "")))
        == norm_log_file
        for h in root_logger.handlers
    )
    if not has_target_file_handler:
        try:
            file_handler = logging.FileHandler(settings.LOG_FILE, encoding="utf-8")
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(logging.Formatter(log_format))
            root_logger.addHandler(file_handler)
        except OSError:
            pass

    scrubber = LogScrubbingFilter(str(Path.home()))
    root_logger.addFilter(scrubber)
    new_handlers: list[logging.Handler] = []
    for handler in root_logger.handlers:
        handler.addFilter(scrubber)
        if handler not in pre_existing_handlers:
            new_handlers.append(handler)
            if tracked_handlers is not None:
                tracked_handlers.append(handler)

    for record in startup_buffer.records:
        if scrubber.filter(record):
            for handler in new_handlers:
                if record.levelno >= handler.level:
                    handler.handle(record)

    return settings


def main():
    """Execute the main application GUI or Demo with CLI exception boundary."""
    try:
        _main_impl()
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:
        active_args = getattr(SortifyArgumentParser, "_active_cli_args", None)
        has_json = (active_args is not None and "--json" in active_args) or (
            "--json" in sys.argv
        )
        if has_json:
            emit_cli_error(str(exc) or type(exc).__name__, json_mode=True, exit_code=1)
        raise


if __name__ == "__main__":
    import multiprocessing

    prepare_frozen_windows_runtime()
    multiprocessing.freeze_support()
    main()
