"""CLI subcommand handler for compliance quarantine staging, inspection, and release."""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List

from app.config import AppSettings
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.quarantine_interceptor import QuarantineInterceptorService

logger = logging.getLogger(__name__)


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
        help="Trigger forensic scanning and policy evaluation for quarantine jobs",
    )
    add_quarantine_common(p_process)
    p_process.add_argument(
        "job_ids_pos",
        nargs="*",
        default=[],
        metavar="JOB_ID",
        help="Quarantine job ID(s)",
    )
    p_process.add_argument(
        "--job-id",
        type=str,
        default=None,
        help="Quarantine job ID",
    )
    p_process.add_argument(
        "--job-ids",
        type=str,
        default=None,
        help="Comma-separated list of quarantine job IDs",
    )
    p_process.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Forensic scan timeout in seconds (default 300.0)",
    )

    # release
    p_release = quarantine_subparsers.add_parser(
        "release", help="Release quarantined items to destination directory"
    )
    add_quarantine_common(p_release)
    p_release.add_argument(
        "job_ids_pos",
        nargs="*",
        default=[],
        metavar="JOB_ID",
        help="Quarantine job ID(s)",
    )
    p_release.add_argument(
        "--job-id",
        type=str,
        default=None,
        help="Quarantine job ID",
    )
    p_release.add_argument(
        "--job-ids",
        type=str,
        default=None,
        help="Comma-separated list of quarantine job IDs",
    )
    p_release.add_argument(
        "--dest-dir",
        type=str,
        default=None,
        help="Target destination directory for released file",
    )

    # dismiss
    p_dismiss = quarantine_subparsers.add_parser(
        "dismiss", help="Dismiss quarantine items without restoring files"
    )
    add_quarantine_common(p_dismiss)
    p_dismiss.add_argument(
        "job_ids_pos",
        nargs="*",
        default=[],
        metavar="JOB_ID",
        help="Quarantine job ID(s)",
    )
    p_dismiss.add_argument(
        "--job-id",
        type=str,
        default=None,
        help="Quarantine job ID",
    )
    p_dismiss.add_argument(
        "--job-ids",
        type=str,
        default=None,
        help="Comma-separated list of quarantine job IDs",
    )


def handle_quarantine_command(args: argparse.Namespace, settings: AppSettings) -> bool:
    """Handle quarantine subcommand execution. Returns True if handled."""
    if getattr(args, "subcommand", None) != "quarantine":
        return False

    quarantine_cmd = getattr(args, "quarantine_command", None)
    if not quarantine_cmd:
        print(
            "Error: Missing quarantine subcommand. Use 'list', 'inspect', 'process', 'release', or 'dismiss'.",
            file=sys.stderr,
        )
        sys.exit(2)

    db_path = getattr(args, "db_path", None)
    if db_path:
        db = Database(Path(db_path).resolve(), DBWorker())
    else:
        from app.core.store import open_store

        db = open_store().database(DBWorker())

    from app.cli import json_default

    quiet = getattr(args, "quiet", False)
    is_json = getattr(args, "json", False)

    def resolve_job_ids(args_obj: argparse.Namespace) -> List[str]:
        job_ids: List[str] = []

        pos_val = getattr(args_obj, "job_ids_pos", None) or getattr(
            args_obj, "job_id_pos", None
        )
        if isinstance(pos_val, list):
            for item in pos_val:
                if item:
                    job_ids.extend([x.strip() for x in str(item).split(",") if x.strip()])
        elif isinstance(pos_val, str) and pos_val:
            job_ids.extend([x.strip() for x in pos_val.split(",") if x.strip()])

        kw_val = getattr(args_obj, "job_ids", None)
        if isinstance(kw_val, list):
            for item in kw_val:
                if item:
                    job_ids.extend([x.strip() for x in str(item).split(",") if x.strip()])
        elif isinstance(kw_val, str) and kw_val:
            job_ids.extend([x.strip() for x in kw_val.split(",") if x.strip()])

        single_kw = getattr(args_obj, "job_id", None)
        if isinstance(single_kw, str) and single_kw:
            job_ids.extend([x.strip() for x in single_kw.split(",") if x.strip()])

        unique_ids = list(dict.fromkeys(job_ids))
        if not unique_ids:
            print(
                "Error: Job ID is required. Specify <job_id ...> or --job-ids <job_ids>.",
                file=sys.stderr,
            )
            sys.exit(2)
        return unique_ids

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
            job_ids = resolve_job_ids(args)
            job_id = job_ids[0]
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
            job_ids = resolve_job_ids(args)
            timeout = getattr(args, "timeout", 300.0)

            service = QuarantineInterceptorService(db=db, worker_timeout=timeout)
            try:
                results = service.process_quarantine_batch(
                    job_ids, timeout_override=timeout
                )
                success_count = sum(
                    1
                    for r in results
                    if (r.get("status") or "").upper() not in ("FAILED", "ERROR")
                )
                res = {
                    "status": (
                        "success"
                        if success_count == len(job_ids)
                        else ("partial_success" if success_count > 0 else "failed")
                    ),
                    "count": len(results),
                    "results": results,
                }

                if is_json:
                    sys.stdout.write(
                        json.dumps(res, indent=2, default=json_default) + "\n"
                    )
                    sys.stdout.flush()
                else:
                    if not quiet:
                        print(
                            f"Quarantine batch process complete: {success_count}/{len(job_ids)} processed successfully.",
                            file=sys.stderr,
                        )
                    for r in results:
                        st = r.get("status", "UNKNOWN")
                        jid = r.get("job_id", "")
                        if st in ("FAILED", "ERROR"):
                            print(
                                f"  [{st}] {jid}: {r.get('error')}", file=sys.stderr
                            )
                        else:
                            print(f"  [{st}] {jid}", file=sys.stderr)

                sys.exit(0 if success_count > 0 else 1)
            except Exception as e:
                logger.debug("Quarantine batch process failed", exc_info=True)
                print(
                    f"Error processing quarantine batch: {e}", file=sys.stderr
                )
                sys.exit(1)

        elif quarantine_cmd == "release":
            job_ids = resolve_job_ids(args)
            dest_dir_override = getattr(args, "dest_dir", None)

            service = QuarantineInterceptorService(db=db)
            try:
                results = service.release_quarantine_batch(
                    job_ids, dest_dir=dest_dir_override
                )
                success_count = sum(
                    1 for r in results if r.get("status") == "RELEASED"
                )
                res = {
                    "status": (
                        "success"
                        if success_count == len(job_ids)
                        else ("partial_success" if success_count > 0 else "failed")
                    ),
                    "count": len(results),
                    "results": results,
                }

                if is_json:
                    sys.stdout.write(
                        json.dumps(res, indent=2, default=json_default) + "\n"
                    )
                    sys.stdout.flush()
                else:
                    if not quiet:
                        print(
                            f"Quarantine batch release complete: {success_count}/{len(job_ids)} released successfully.",
                            file=sys.stderr,
                        )
                    for r in results:
                        st = r.get("status", "UNKNOWN")
                        jid = r.get("job_id", "")
                        dest = r.get("destination", "")
                        if st == "RELEASED":
                            print(f"  [{st}] {jid} -> {dest}", file=sys.stderr)
                        else:
                            print(
                                f"  [{st}] {jid}: {r.get('error') or r.get('details')}",
                                file=sys.stderr,
                            )

                sys.exit(0 if success_count > 0 else 1)
            except Exception as e:
                logger.debug("Quarantine batch release failed", exc_info=True)
                print(f"Error releasing quarantine batch: {e}", file=sys.stderr)
                sys.exit(1)

        elif quarantine_cmd == "dismiss":
            job_ids = resolve_job_ids(args)

            service = QuarantineInterceptorService(db=db)
            try:
                results = service.dismiss_quarantine_batch(job_ids)
                success_count = sum(
                    1 for r in results if r.get("status") == "DISMISSED"
                )
                res = {
                    "status": (
                        "success"
                        if success_count == len(job_ids)
                        else ("partial_success" if success_count > 0 else "failed")
                    ),
                    "count": len(results),
                    "results": results,
                }

                if is_json:
                    sys.stdout.write(
                        json.dumps(res, indent=2, default=json_default) + "\n"
                    )
                    sys.stdout.flush()
                else:
                    if not quiet:
                        print(
                            f"Quarantine batch dismiss complete: {success_count}/{len(job_ids)} dismissed successfully.",
                            file=sys.stderr,
                        )
                    for r in results:
                        st = r.get("status", "UNKNOWN")
                        jid = r.get("job_id", "")
                        if st == "DISMISSED":
                            print(f"  [{st}] {jid}", file=sys.stderr)
                        else:
                            print(
                                f"  [{st}] {jid}: {r.get('error') or r.get('details')}",
                                file=sys.stderr,
                            )

                sys.exit(0 if success_count > 0 else 1)
            except Exception as e:
                logger.debug("Quarantine batch dismiss failed", exc_info=True)
                print(f"Error dismissing quarantine batch: {e}", file=sys.stderr)
                sys.exit(1)

    finally:
        if db and hasattr(db, "worker") and db.worker:
            db.worker.stop()
        from app.core.db_conn import clear_connection_cache

        clear_connection_cache(only_current_and_inactive=False)

    return True
