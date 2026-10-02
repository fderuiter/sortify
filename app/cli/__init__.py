"""Modular CLI subcommand package and domain registry."""

import argparse
import dataclasses
from pathlib import Path
from typing import Any

from app.cli.ledger_cli import handle_ledger_command
from app.cli.ledger_cli import register_subparser as register_ledger
from app.cli.quarantine_cli import handle_quarantine_command
from app.cli.quarantine_cli import register_subparser as register_quarantine
from app.config import AppSettings


def json_default(obj: Any) -> Any:
    """Fallback JSON serializer for Pydantic models, dataclasses, Path objects, and sets."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if hasattr(obj, "dict"):
        return obj.dict()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, tuple)):
        return list(obj)
    return str(obj)


def register_cli_subcommands(subparsers: argparse._SubParsersAction) -> None:
    """Register core domain subcommand subparsers into the central parser."""
    register_ledger(subparsers)
    register_quarantine(subparsers)


def handle_cli_command(args: argparse.Namespace, settings: AppSettings) -> bool:
    """Dispatch command to domain CLI handlers. Returns True if handled."""
    subcmd = getattr(args, "subcommand", None)
    if subcmd == "ledger":
        return handle_ledger_command(args, settings)
    elif subcmd == "quarantine":
        return handle_quarantine_command(args, settings)
    return False


__all__ = ["register_cli_subcommands", "handle_cli_command", "json_default"]
