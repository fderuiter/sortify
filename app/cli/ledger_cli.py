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


def handle_ledger_command(args: argparse.Namespace, settings: AppSettings) -> bool:
    """Handle ledger subcommand execution. Returns True if handled."""
    if getattr(args, "subcommand", None) != "ledger":
        return False

    ledger_cmd = getattr(args, "ledger_command", None)
    if not ledger_cmd:
        print(
            "Error: Missing ledger subcommand. Use 'status', 'reconcile', or 'purge'.",
            file=sys.stderr,
        )
        sys.exit(2)

    ledger_db_path = getattr(args, "ledger_db", None) or get_default_ledger_path()
    ledger = TransactionLedger(db_path=ledger_db_path)

    quiet = getattr(args, "quiet", False)
    is_json = getattr(args, "json", False)

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
        db = None
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
                    "Transaction ledger records purged successfully.", file=sys.stderr
                )

        sys.exit(0)

    return True
