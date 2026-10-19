"""Numbered schema migrations for Sortify's SQLite databases (ADR 0008).

Each database file has one ordered list of migrations. A migration runs once,
inside a transaction, and moves ``PRAGMA user_version`` to its own number, so a
failure leaves the file at its previous version. Migrations are never edited
after a release; a schema change is a new migration at the end of the list.
"""

import logging
from dataclasses import dataclass
from typing import Callable, Sequence


class StoreTooNewError(RuntimeError):
    """Raised when a database was written by a newer Sortify than this one."""


@dataclass(frozen=True)
class Migration:
    """One schema change: its version number, a short name and the function that applies it."""

    version: int
    name: str
    apply: Callable[[object], None]


def schema_version(conn) -> int:
    """Return the database's ``PRAGMA user_version``."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def table_columns(conn, table: str) -> set[str]:
    """Return the column names of ``table``, or an empty set if it does not exist."""
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def add_missing_columns(conn, table: str, columns: dict[str, str]) -> None:
    """Add each column in ``columns`` (name to SQL type) that ``table`` lacks."""
    existing = table_columns(conn, table)
    for name, sql_type in columns.items():
        if name not in existing:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {sql_type}')


def _check_order(migrations: Sequence[Migration]) -> None:
    versions = [m.version for m in migrations]
    if not versions:
        raise ValueError("A migration list needs at least one migration.")
    if any(v <= 0 for v in versions) or versions != sorted(set(versions)):
        raise ValueError(
            f"Migration versions must be positive and strictly increasing: {versions}"
        )


def migrate(conn, migrations: Sequence[Migration], *, label: str) -> int:
    """Apply every migration newer than the database's version, in order.

    Returns the version the database ends at. Raises ``StoreTooNewError`` without
    changing anything when the database is newer than the newest migration.
    """
    _check_order(migrations)
    head = migrations[-1].version
    current = schema_version(conn)
    if current > head:
        raise StoreTooNewError(
            f"{label} is at schema version {current}, but this version of Sortify "
            f"only knows up to {head}. Update Sortify to open it."
        )

    if conn.in_transaction:
        conn.commit()
    for migration in migrations:
        if migration.version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        # Another connection may have applied it while this one waited for the lock.
        if schema_version(conn) >= migration.version:
            conn.rollback()
            current = schema_version(conn)
            continue
        try:
            migration.apply(conn)
            conn.execute(f"PRAGMA user_version = {int(migration.version)}")
            conn.commit()
        except BaseException:
            conn.rollback()
            logging.error(
                f"Migration {migration.version} ({migration.name}) failed on {label}; "
                f"it stays at version {current}."
            )
            raise
        logging.info(
            f"Migrated {label} to version {migration.version} ({migration.name})."
        )
        current = migration.version
    return current
