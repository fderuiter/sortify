---
status: accepted (2026-10-19)
---

# One app store, versioned by numbered migrations

Sortify keeps its state in one encrypted SQLite store under `~/.autosorter/`, not in per-session databases in the system temp folder. Sessions are rows in that store, alongside `documents`, `vectors`, `moves` (ADR 0006), `quarantine_records` and `settings_audit`. Every connection runs `PRAGMA foreign_keys=ON`, so deleting a document cascades to its vectors.

The schema has one version, held in `PRAGMA user_version`. Changes are numbered migration functions in one module (`0001_initial`, `0002_…`), each run once, in order, inside a transaction, and never edited after release. Opening a store newer than the code refuses with a clear message rather than guessing. Extraction status is a column on `documents`, not a `[STATUS:FAILED]` string stored as the document text, so failed extractions can be retried when the extractor changes.

We chose this because schema changes today are ad hoc in four modules (#712): `db.py` checks `user_version` for some changes and then runs `ALTER TABLE documents ADD COLUMN rating` inside `try/except: pass` on every open, and `history.py` does the same for ten `snapshot_files` columns. Errors other than "column exists" are swallowed with them. Foreign keys are never enabled, so `ON DELETE CASCADE` on `document_vectors` does nothing and deletes leave encrypted vectors behind. Per-session databases in the temp folder mean undo history can vanish on reboot, and the `ledger` and `quarantine` CLIs read a different database from the one sorts write to.

## Considered options

- **Alembic.** Mature, but it brings SQLAlchemy, its own config and its own history table, and it has to be taught about SQLCipher keys. A dozen small functions and `user_version` cover a single-user embedded store.
- **Keep per-session databases, add migrations to each.** Keeps cleanup as deleting a folder, but every session database needs migrating, and queries across sessions (history, ledger, quarantine) still have to open many files.
- **Keep `try/ALTER/except`.** Rejected: it can't express anything but adding a column, it hides real errors, and it runs on every open.

## Consequences

- Undo history survives a reboot, and every CLI and screen sees the same sessions.
- One key per store, instead of a keyring entry per session database.
- The store grows over time, so it needs a retention setting and a purge command. Deleting a session becomes a `DELETE` plus a `VACUUM`, not `rm -r`.
- Existing session databases need a one-time import, done by the first migration and shared with the ledger import in ADR 0006.
- Enabling foreign keys can make existing orphan rows fail checks, so the first migration deletes orphans before turning the pragma on.
- A failing migration rolls back and leaves the store at its previous version, so a bad release cannot half-upgrade it.
