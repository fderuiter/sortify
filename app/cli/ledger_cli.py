"""CLI subcommand handler for transaction ledger status, reconciliation, and purging."""

import argparse
import json
import sys

from app.config import AppSettings, get_app_dir
from app.core.db import Database
from app.core.ledger import TransactionLedger, get_default_ledger_path


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ledger subcommand group."""
    parser_ledger = subparsers.add_parser(
        "ledger",
        help="Manage transaction ledger entries and automated reconciliation",
    )
    ledger_subparsers = parser_ledger.add_subparsers(
        dest="ledger_command", help="Ledger subcommands"
    )

    def add_ledger_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--ledger-db",
            type=str,
            default=None,
            help="Path to sidecar transaction ledger database file",
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

    p_status = ledger_subparsers.add_parser(
        "status", help="Display incomplete or pending transaction ledger entries"
    )
    add_ledger_common(p_status)
    p_status.add_argument(
        "--session-id",
        type=str,
        default=None,
        help="Filter pending entries by session ID",
    )

    p_reconcile = ledger_subparsers.add_parser(
        "reconcile",
        help="Execute automated headless reconciliation for interrupted file moves",
    )
    add_ledger_common(p_reconcile)

    p_purge = ledger_subparsers.add_parser(
        "purge", help="Purge completed or session transaction ledger records"
    )
    add_ledger_common(p_purge)
    p_purge.add_argument(
        "--session-id",
        type=str,
        default=None,
        help="Purge entries for a specific session ID",
    )
    p_purge.add_argument(
        "--completed",
        action="store_true",
        help="Purge all completed transaction records",
    )
    p_purge.add_argument(
        "--force",
        action="store_true",
        help="Bypass confirmation prompt when purging records",
    )

    p_export = ledger_subparsers.add_parser(
        "export",
        help="Export session history transaction records as CSV or JSON audit log",
    )
    add_ledger_common(p_export)
    p_export.add_argument(
        "--session-id",
        type=str,
        default=None,
        help="Export entries for a specific session ID",
    )
    p_export.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        help="Output file path for exported audit log",
    )
    p_export.add_argument(
        "-f",
        "--format",
        type=str,
        choices=["csv", "json"],
        default=None,
        help="Audit log export format (csv or json)",
    )
    p_export.add_argument(
        "--csv",
        action="store_true",
        help="Export audit log in CSV format",
    )


def handle_ledger_command(args: argparse.Namespace, settings: AppSettings) -> bool:
    """Handle ledger subcommand execution. Returns True if handled."""
    if getattr(args, "subcommand", None) != "ledger":
        return False

    ledger_cmd = getattr(args, "ledger_command", None)
    if not ledger_cmd:
        print(
            "Error: Missing ledger subcommand. Use 'status', 'reconcile', 'purge', or 'export'.",
            file=sys.stderr,
        )
        sys.exit(2)

    ledger_db_path = getattr(args, "ledger_db", None) or get_default_ledger_path()
    ledger = TransactionLedger(db_path=ledger_db_path)

    quiet = getattr(args, "quiet", False)
    is_json = getattr(args, "json", False)

    db = None
    try:
        if ledger_cmd == "status":
            session_id = getattr(args, "session_id", None)
            pending = ledger.get_pending_entries(session_id=session_id)

            res = {
                "status": "success",
                "count": len(pending),
                "pending_entries": pending,
            }

            if is_json:
                sys.stdout.write(json.dumps(res, indent=2) + "\n")
                sys.stdout.flush()
            else:
                if not quiet:
                    print(
                        f"Transaction Ledger Status: {len(pending)} pending or incomplete entry(ies)."
                    )
                for entry in pending:
                    print(
                        f"  [{entry.get('status')}] {entry.get('entry_id')} | "
                        f"Source: {entry.get('source_path')} -> Dest: {entry.get('dest_path')}"
                    )

            sys.exit(0)

        elif ledger_cmd == "reconcile":
            try:
                from app.core.db_worker import DBWorker

                db = Database(get_app_dir() / "autosorter.db", DBWorker())
            except Exception:
                pass

            reconciled_count = ledger.reconcile_incomplete_transactions(db=db)

            res = {
                "status": "success",
                "reconciled_count": reconciled_count,
            }

            if is_json:
                sys.stdout.write(json.dumps(res, indent=2) + "\n")
                sys.stdout.flush()
            else:
                if not quiet:
                    print(
                        f"Transaction ledger reconciliation completed. Reconciled {reconciled_count} entry(ies).",
                        file=sys.stderr,
                    )

            sys.exit(0)

        elif ledger_cmd == "purge":
            session_id = getattr(args, "session_id", None)
            completed_only = getattr(args, "completed", False)
            force = getattr(args, "force", False)

            if not completed_only and not session_id and not force:
                if not sys.stdin.isatty():
                    print(
                        "Error: Purging all ledger records requires explicit confirmation. Use --force or --completed.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                response = input(
                    "Are you sure you want to purge transaction ledger records? [y/N]: "
                )
                if response.strip().lower() not in ("y", "yes"):
                    print("Purge operation cancelled.", file=sys.stderr)
                    sys.exit(1)

            if session_id:
                ledger.purge_session(session_id)
            else:
                ledger.purge_completed()

            res = {
                "status": "success",
                "message": "Transaction ledger records purged successfully.",
                "session_id": session_id,
                "completed_only": completed_only or not session_id,
            }

            if is_json:
                sys.stdout.write(json.dumps(res, indent=2) + "\n")
                sys.stdout.flush()
            else:
                if not quiet:
                    print(
                        "Transaction ledger records purged successfully.",
                        file=sys.stderr,
                    )

            sys.exit(0)

        elif ledger_cmd == "export":
            session_id = getattr(args, "session_id", None)
            output_path = getattr(args, "output", None)
            fmt = getattr(args, "format", None)
            is_csv = getattr(args, "csv", False)

            if is_csv and not fmt:
                fmt = "csv"

            if not output_path:
                ext = ".csv" if fmt == "csv" else ".json"
                output_path = f"audit_log_{session_id or 'all'}{ext}"

            from app.core.cache import CacheManager
            from app.core.db_worker import DBWorker
            from app.core.history import HistoryManager
            from app.main import find_all_history_sessions

            target_db_path = None
            all_sessions = find_all_history_sessions()
            if session_id:
                for s in all_sessions:
                    if s.get("session_id") == session_id:
                        target_db_path = s.get("history_db_path")
                        break
            if not target_db_path and all_sessions:
                target_db_path = all_sessions[0].get("history_db_path")
            if not target_db_path:
                target_db_path = str(get_app_dir() / "history.db")

            worker = DBWorker()
            try:
                db = Database(get_app_dir() / "autosorter.db", worker)
                cache_mgr = CacheManager(str(get_app_dir() / "cache.db"), worker)
                history_mgr = HistoryManager(db, cache_mgr, target_db_path)
                export_res = history_mgr.export_audit_log(
                    output_path=output_path,
                    session_id=session_id,
                    format=fmt,
                )

                if is_json:
                    sys.stdout.write(json.dumps(export_res, indent=2) + "\n")
                    sys.stdout.flush()
                else:
                    if not quiet:
                        print(
                            f"Audit log exported ({export_res.get('format', '').upper()}) to '{output_path}'. {export_res.get('count', 0)} record(s) written.",
                            file=sys.stderr,
                        )
                sys.exit(0)
            except ValueError as ve:
                err_res = {"status": "error", "message": str(ve)}
                if is_json:
                    sys.stdout.write(json.dumps(err_res, indent=2) + "\n")
                    sys.stdout.flush()
                else:
                    print(f"Error: {ve}", file=sys.stderr)
                sys.exit(1)

    finally:
        if db and hasattr(db, "worker") and db.worker:
            db.worker.stop()
        from app.core.db_conn import clear_connection_cache

        clear_connection_cache(only_current_and_inactive=False)

    return True
