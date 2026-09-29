"""Modular CLI subcommand package and domain registry."""

import argparse

from app.cli.cro_cli import handle_cro_command
from app.cli.cro_cli import register_subparser as register_cro
from app.cli.crypto_cli import handle_crypto_command
from app.cli.crypto_cli import register_subparser as register_crypto
from app.cli.ledger_cli import handle_ledger_command
from app.cli.ledger_cli import register_subparser as register_ledger
from app.cli.quarantine_cli import handle_quarantine_command
from app.cli.quarantine_cli import register_subparser as register_quarantine
from app.config import AppSettings


def register_cli_subcommands(subparsers: argparse._SubParsersAction) -> None:
    """Register all modular domain subcommand subparsers into the central parser."""
    register_crypto(subparsers)
    register_ledger(subparsers)
    register_quarantine(subparsers)
    register_cro(subparsers)


def handle_cli_command(args: argparse.Namespace, settings: AppSettings) -> bool:
    """Dispatch command to domain CLI handlers. Returns True if handled."""
    subcmd = getattr(args, "subcommand", None)
    if subcmd == "crypto":
        return handle_crypto_command(args, settings)
    elif subcmd == "ledger":
        return handle_ledger_command(args, settings)
    elif subcmd == "quarantine":
        return handle_quarantine_command(args, settings)
    elif subcmd == "cro":
        return handle_cro_command(args, settings)
    return False


__all__ = ["register_cli_subcommands", "handle_cli_command"]
