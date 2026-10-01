"""CLI subcommand handler for compliance quarantine staging, inspection, and release."""

import argparse
import json
import os
import sys
from pathlib import Path

from app.config import AppSettings, get_app_dir
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.quarantine_interceptor import QuarantineInterceptorService
from app.core.resilient_file_ops import resilient_move


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register quarantine subcommand group."""
    parser_quarantine = subparsers.add_parser(
        "quarantine",
        help="Manage compliance quarantine staging, inspection, and release",
    )
    quarantine_subparsers = parser_quarantine.add_subparsers(
        dest="quarantine_command", help="Quarantine subcommands"
    )

    def add_quarantine_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--db-path",
            type=str,
            default=None,
            help="Path to database file",
        )
        p.add_argument(
            "-q",
            "--quiet",
            action="store_true",
            dest="quiet",
            default=argparse.SUPPRESS,
            help="Suppress informational prints",
        )
        p.add_argument(
            "--no-color",
            action="store_true",
            dest="no_color",
            default=argparse.SUPPRESS,
            help="Disable ANSI color formatting",
        )
        p.add_argument(
            "--json",
            action="store_true",
            help="Output response in structured JSON format",
        )

    # list
    p_list = quarantine_subparsers.add_parser(
        "list", help="List staged and quarantined compliance items"
    )
    add_quarantine_common(p_list)
    p_list.add_argument(
        "--status",
        type=str,
        default=None,
        help="Filter items by quarantine status (e.g. STAGED, QUARANTINED, IN_INSPECTION, RELEASED, DEAD_LETTER_QUEUE)",
    )
    p_list.add_argument(
        "--base-dir",
        type=str,
        default=None,
        help="Filter items by base directory",
    )

    # inspect
    p_inspect = quarantine_subparsers.add_parser(
        "inspect", help="Inspect a specific quarantine job record and audit trail"
    )
    add_quarantine_common(p_inspect)
    p_inspect.add_argument(
        "job_id_pos",
        nargs="?",
        default=None,
        metavar="JOB_ID",
        help="Quarantine job ID",
    )
    p_inspect.add_argument(
        "--job-id",
        type=str,
        default=None,
        help="Quarantine job ID",
    )

    # process
    p_process = quarantine_subparsers.add_parser(
        "process",
        help="Trigger forensic scanning and policy evaluation for a quarantine job",
    )
    add_quarantine_common(p_process)
    p_process.add_argument(
        "job_id_pos",
        nargs="?",
        default=None,
        metavar="JOB_ID",
        help="Quarantine job ID",
    )
    p_process.add_argument(
        "--job-id",
        type=str,
        default=None,
        help="Quarantine job ID",
    )
    p_process.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Forensic scan timeout in seconds (default 300.0)",
    )

    # release
    p_release = quarantine_subparsers.add_parser(
        "release", help="Release a quarantined item to its destination directory"
    )
    add_quarantine_common(p_release)
    p_release.add_argument(
        "job_id_pos",
        nargs="?",
        default=None,
        metavar="JOB_ID",
        help="Quarantine job ID",
    )
    p_release.add_argument(
        "--job-id",
        type=str,
        default=None,
        help="Quarantine job ID",
    )
    p_release.add_argument(
        "--dest-dir",
        type=str,
        default=None,
        help="Target destination directory for released file",
    )


def handle_quarantine_command(args: argparse.Namespace, settings: AppSettings) -> bool:
    """Handle quarantine subcommand execution. Returns True if handled."""
    if getattr(args, "subcommand", None) != "quarantine":
        return False

    quarantine_cmd = getattr(args, "quarantine_command", None)
    if not quarantine_cmd:
        print(
            "Error: Missing quarantine subcommand. Use 'list', 'inspect', 'process', or 'release'.",
            file=sys.stderr,
        )
        sys.exit(2)

    db_path = getattr(args, "db_path", None)
    db_path_obj = (
        Path(db_path).resolve() if db_path else (get_app_dir() / "autosorter.db")
    )
    db = Database(db_path_obj, DBWorker())

    from app.cli import json_default

    quiet = getattr(args, "quiet", False)
    is_json = getattr(args, "json", False)

    def resolve_job_id(args_obj: argparse.Namespace) -> str:
        job_id = getattr(args_obj, "job_id", None) or getattr(
            args_obj, "job_id_pos", None
        )
        if not job_id:
            print(
                "Error: Job ID is required. Specify <job_id> or --job-id <job_id>.",
                file=sys.stderr,
            )
            sys.exit(2)
        return job_id

    try:
        if quarantine_cmd == "list":
            base_dir = getattr(args, "base_dir", None)
            status_filter = getattr(args, "status", None)

            if base_dir:
                records = db.get_quarantine_records_by_base_dir(
                    base_dir, status=status_filter
                )
            else:
                records = db.get_all_quarantine_records()
                if status_filter:
                    status_upper = status_filter.upper()
                    records = [r for r in records if r.get("status") == status_upper]

            res = {
                "status": "success",
                "count": len(records),
                "items": records,
            }

            if is_json:
                sys.stdout.write(json.dumps(res, indent=2, default=json_default) + "\n")
                sys.stdout.flush()
            else:
                if not quiet:
                    print(f"Quarantine Staging: {len(records)} record(s) found.")
                for r in records:
                    print(
                        f"  [{r.get('status')}] {r.get('job_id')} | "
                        f"Original: {r.get('original_filepath')} | Staged: {r.get('staged_filepath')}"
                    )

            sys.exit(0)

        elif quarantine_cmd == "inspect":
            job_id = resolve_job_id(args)
            record = db.get_quarantine_record(job_id)

            if not record:
                print(
                    f"Error: Quarantine record not found for job ID '{job_id}'.",
                    file=sys.stderr,
                )
                sys.exit(1)

            res = {
                "status": "success",
                "record": record,
            }

            if is_json:
                sys.stdout.write(json.dumps(res, indent=2, default=json_default) + "\n")
                sys.stdout.flush()
            else:
                print(f"Quarantine Job Record: {job_id}")
                print(f"  Status: {record.get('status')}")
                print(f"  Base Dir: {record.get('base_dir')}")
                print(f"  Original Filepath: {record.get('original_filepath')}")
                print(f"  Staged Filepath: {record.get('staged_filepath')}")
                print(f"  Policy Action: {record.get('policy_action')}")
                if record.get("error_message"):
                    print(f"  Error Message: {record.get('error_message')}")

                audit_log = record.get("audit_log", [])
                if audit_log:
                    print("  Audit Log:")
                    for entry in audit_log:
                        print(f"    - [{entry.get('status')}] {entry.get('details')}")

            sys.exit(0)

        elif quarantine_cmd == "process":
            job_id = resolve_job_id(args)
            timeout = getattr(args, "timeout", 300.0)

            service = QuarantineInterceptorService(db=db, worker_timeout=timeout)
            try:
                result = service.process_quarantine_job(job_id)
                res = {
                    "status": "success",
                    "result": result,
                }

                if is_json:
                    sys.stdout.write(
                        json.dumps(res, indent=2, default=json_default) + "\n"
                    )
                    sys.stdout.flush()
                else:
                    if not quiet:
                        print(
                            f"Quarantine job '{job_id}' processed. "
                            f"New Status: {result.get('status')}",
                            file=sys.stderr,
                        )

                sys.exit(0)
            except Exception as e:
                print(
                    f"Error processing quarantine job '{job_id}': {e}", file=sys.stderr
                )
                sys.exit(1)

        elif quarantine_cmd == "release":
            job_id = resolve_job_id(args)
            record = db.get_quarantine_record(job_id)

            if not record:
                print(
                    f"Error: Quarantine record not found for job ID '{job_id}'.",
                    file=sys.stderr,
                )
                sys.exit(1)

            staged_path = record.get("staged_filepath")
            base_dir = record.get("base_dir") or ""
            orig_rel = record.get("original_filepath") or "released_file"
            dest_dir_override = getattr(args, "dest_dir", None)

            if dest_dir_override:
                target_dir = Path(dest_dir_override).resolve()
            else:
                target_dir = Path(base_dir).resolve() if base_dir else Path.cwd()

            target_dir.mkdir(parents=True, exist_ok=True)
            dest_file_path = target_dir / os.path.basename(orig_rel)

            if staged_path and os.path.exists(staged_path):
                if os.path.abspath(staged_path) != os.path.abspath(dest_file_path):
                    resilient_move(staged_path, str(dest_file_path))

            db.update_quarantine_status(
                job_id=job_id,
                status="RELEASED",
                policy_action="release",
                audit_entry={
                    "timestamp": __import__("time").time(),
                    "status": "RELEASED",
                    "details": f"Manually released from quarantine to {dest_file_path}",
                },
            )

            res = {
                "status": "success",
                "job_id": job_id,
                "status_code": "RELEASED",
                "destination": str(dest_file_path),
            }

            if is_json:
                sys.stdout.write(json.dumps(res, indent=2, default=json_default) + "\n")
                sys.stdout.flush()
            else:
                if not quiet:
                    print(
                        f"Quarantine item '{job_id}' released successfully to '{dest_file_path}'.",
                        file=sys.stderr,
                    )

            sys.exit(0)

    finally:
        if db and hasattr(db, "worker") and db.worker:
            db.worker.stop()
        from app.core.db_conn import clear_connection_cache

        clear_connection_cache(only_current_and_inactive=False)

    return True
