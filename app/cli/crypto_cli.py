"""CLI subcommand handler for database encryption keys and cryptographic status."""

import argparse
import json
import os
import sys
from pathlib import Path

from app.config import AppSettings, get_app_dir
from app.core.crypto import SessionCrypto


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register crypto subcommand group."""
    parser_crypto = subparsers.add_parser(
        "crypto",
        help="Manage database encryption keys and cryptographic status",
    )
    crypto_subparsers = parser_crypto.add_subparsers(
        dest="crypto_command", help="Crypto subcommands"
    )

    def add_crypto_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--db",
            type=str,
            default=None,
            help="Path to database file (defaults to active application database)",
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

    p_info = crypto_subparsers.add_parser(
        "info", help="Report active encryption key location and storage backend"
    )
    add_crypto_common(p_info)

    p_rotate = crypto_subparsers.add_parser(
        "rotate-key",
        help="Safely re-encrypt database keys with new Fernet/SQLCipher key",
    )
    add_crypto_common(p_rotate)
    p_rotate.add_argument(
        "--force",
        action="store_true",
        help="Bypass explicit confirmation prompt for destructive key rotation",
    )

    p_export = crypto_subparsers.add_parser(
        "export-key", help="Export active raw encryption key"
    )
    add_crypto_common(p_export)
    p_export.add_argument(
        "--output",
        type=str,
        default=None,
        help="Target file path to export key to",
    )
    p_export.add_argument(
        "--force",
        action="store_true",
        help="Bypass confirmation prompt when exporting secret key",
    )


def handle_crypto_command(args: argparse.Namespace, settings: AppSettings) -> bool:
    """Handle crypto subcommand execution. Returns True if handled."""
    if getattr(args, "subcommand", None) != "crypto":
        return False

    crypto_cmd = getattr(args, "crypto_command", None)
    if not crypto_cmd:
        print(
            "Error: Missing crypto subcommand. Use 'info', 'rotate-key', or 'export-key'.",
            file=sys.stderr,
        )
        sys.exit(2)

    db_path = (
        Path(args.db).resolve()
        if getattr(args, "db", None)
        else get_app_dir() / "autosorter.db"
    )
    key_path = db_path.parent / f"{db_path.name}.key"
    session_crypto = SessionCrypto(key_path=key_path, db_path=db_path)

    quiet = getattr(args, "quiet", False)
    is_json = getattr(args, "json", False)

    try:
        if crypto_cmd == "info":
            key_exists = session_crypto.isolated_key_path.exists()
            import keyring

            keyring_val = None
            try:
                keyring_val = keyring.get_password(
                    session_crypto.keyring_service, session_crypto.keyring_account
                )
            except Exception:
                pass

            storage_backend = (
                "keyring" if keyring_val else ("file" if key_exists else "generated")
            )
            key_path_str = str(session_crypto.isolated_key_path)

            res = {
                "status": "success",
                "key_path": key_path_str,
                "storage_backend": storage_backend,
                "db_path": str(db_path),
            }

            if is_json:
                sys.stdout.write(json.dumps(res, indent=2) + "\n")
                sys.stdout.flush()
            else:
                if not quiet:
                    print(f"Encryption Key Path: {key_path_str}")
                    print(f"Storage Backend: {storage_backend}")
                    print(f"Database Path: {db_path}")

            sys.exit(0)

        elif crypto_cmd == "rotate-key":
            force = getattr(args, "force", False)
            if not force:
                if not sys.stdin.isatty():
                    print(
                        "Error: Key rotation requires explicit confirmation. Use --force in non-interactive mode.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                response = input(
                    "Are you sure you want to rotate the database encryption key? [y/N]: "
                )
                if response.strip().lower() not in ("y", "yes"):
                    print("Key rotation cancelled.", file=sys.stderr)
                    sys.exit(1)

            try:
                session_crypto.rotate_key()
                res = {
                    "status": "success",
                    "message": "Database encryption key rotated successfully.",
                    "key_path": str(session_crypto.isolated_key_path),
                    "db_path": str(db_path),
                }
                if is_json:
                    sys.stdout.write(json.dumps(res, indent=2) + "\n")
                    sys.stdout.flush()
                else:
                    if not quiet:
                        print(
                            f"Database encryption key rotated successfully for '{db_path}'.",
                            file=sys.stderr,
                        )
                        print(
                            f"Updated key stored at: {session_crypto.isolated_key_path}",
                            file=sys.stderr,
                        )
                sys.exit(0)
            except Exception as e:
                print(f"Error during key rotation: {e}", file=sys.stderr)
                sys.exit(1)

        elif crypto_cmd == "export-key":
            raw_key = session_crypto.get_raw_key()
            out_path = getattr(args, "output", None)

            if out_path:
                p = Path(out_path).resolve()
                p.parent.mkdir(parents=True, exist_ok=True)
                try:
                    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "wb") as f:
                        f.write(raw_key.encode("utf-8"))
                except OSError:
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(raw_key)
                    try:
                        os.chmod(p, 0o600)
                    except OSError:
                        pass

                res = {
                    "status": "success",
                    "exported_to": str(p),
                }
                if is_json:
                    sys.stdout.write(json.dumps(res, indent=2) + "\n")
                    sys.stdout.flush()
                else:
                    if not quiet:
                        print(f"Key exported successfully to '{p}'.", file=sys.stderr)
                sys.exit(0)
            else:
                res = {
                    "status": "success",
                    "key": raw_key,
                }
                if is_json:
                    sys.stdout.write(json.dumps(res, indent=2) + "\n")
                    sys.stdout.flush()
                else:
                    print(raw_key)
                sys.exit(0)
    finally:
        from app.core.db_conn import clear_connection_cache

        clear_connection_cache(only_current_and_inactive=False)

    return True
