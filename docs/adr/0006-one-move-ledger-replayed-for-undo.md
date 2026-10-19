---
status: proposed (2026-10-19)
---

# One move ledger, and undo replays it

Every move a sort makes is recorded in one append-only `moves` table, and undo, rollback and crash reconciliation all work by replaying that table. Nothing else records moves. Each row holds `session_id`, `seq`, `src`, `dst`, `src_sha256`, `size`, `displaced` (where an existing file at `dst` was moved aside to, per ADR 0005) and `state`, which goes `intent` → `done` → `verified`, or `failed`. The mover writes `intent` through `DBWorker` (ADR 0003) and commits it before it touches the filesystem, then updates the row after the move.

Undo walks a session's `verified` and `done` rows backwards. For each one it moves the file back only if the file at `dst` still has `src_sha256` and `src` is free; otherwise it reports a conflict and leaves both files. Undo never deletes a file it did not create. Rollback is the same undo, started only after every worker in the sort has stopped. Reconciliation on start-up resolves each `intent` row by finding which path holds the hash.

We chose this because moves are recorded in three places today (`transaction_ledger` in the main database and again in a sidecar `transaction_ledger.db` with a different schema, `history.db`'s `step_ledger`, plus `rollback_journal.json`), and they can disagree after a crash (#714). Undo instead restores from a pre-sort snapshot of the folder and deletes what the snapshot lacks, which is how it has destroyed hidden files and files created after the sort (#713). A ledger of what Sortify itself did can only ever reverse Sortify's own actions.

## Considered options

- **Keep the snapshot and fix its edge cases.** Rejected: a snapshot describes the folder, not Sortify's actions, so restoring it has to decide what to do with every file that changed for any reason. Each fix (#713, #715, #767) narrows the damage without removing the cause.
- **Keep the three ledgers and add a consistency check.** Rejected: it keeps three writers to keep in step, and the check would need the same replay logic anyway.
- **Event sourcing for the whole session store.** More than this needs. Moves are the only state that must be replayable.

## Consequences

- `_rollback_from_snapshot_internal` (about 790 lines), the snapshot tables in `history.db` and `rollback_journal.json` go away, and so does the test-only `_execute_moves_recursive`.
- Sessions written before the change still have only the old ledgers. They need a one-time import into `moves`, or a read-only legacy undo that is retired after a release or two.
- One extra committed write per move. `DBWorker` batches them, and per ADR 0003 no file I/O happens inside the write.
- The `ledger`, `undo` and `quarantine` CLIs and the TUI history screen read the same table, so they agree with what the sort did.
