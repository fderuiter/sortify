---
status: accepted (2026-10-19)
---

# A move never destroys bytes it has not verified exist elsewhere

Every file mutation a sort makes goes through `app/core/resilient_file_ops.py`, and each one obeys a single rule: Sortify deletes or replaces a file only after it has proved that the same bytes are durable somewhere the ledger knows about. Concretely:

1. **No-replace publish.** A file or folder reaches its target path through a rename that fails atomically if the target exists: `renameat2(RENAME_NOREPLACE)` on Linux and `renamex_np(RENAME_EXCL)` on macOS, and `os.rename` on Windows, which never replaces. Where the filesystem lacks the native call, files fall back to `os.link(src, dst)` then `os.unlink(src)`, and folders to an exclusive `mkdir` reservation. Only filesystems with neither (`EPERM`/`ENOTSUP`/`EMLINK` from `link`, as on FAT/exFAT) fall back to check-then-rename under a process-wide lock. The copy fallback runs only on `EXDEV`, never on any `OSError`.
2. **Verified cross-volume copy.** Copy to a staging file in the target folder, `fsync` the file and its folder, compare a content hash with the source, publish the stage with the same no-replace primitive, and only then remove the source.
3. **Overwrite displaces, it does not delete.** The `overwrite` conflict policy first moves the existing target into the session's backup folder, recorded in the ledger, and undo restores it. The backup is purged with the session's history, not at move time.
4. **Rollback waits.** When a move fails, the mover cancels pending futures and waits for running ones before rollback starts, and rollback replays the ledger for the real session id. With no session id it stops and reports instead of unwinding a random one.
5. **Reconciliation checks hashes.** Ledger reconciliation removes a file at a destination only when its hash matches the `file_hash` the ledger recorded before the move; otherwise it leaves both files and reports a conflict.

We chose this because the five highest risks in the codebase today are all ways the current mover can lose a user's file: `atomic_move_non_clobber` checks `lexists` then calls `os.rename`, which replaces silently on POSIX, and treats any `OSError` as a cue to copy; overwrite calls `resilient_remove(dest)` before the move that might fail; cross-volume moves compare only sizes; `fut.result()` raises on the first failure while the rest of the chunk keeps moving and `unwind_session` starts underneath it; and reconciliation deletes the destination when both paths exist. Sortify's whole promise is that a sort can be undone, so one irreversible loss outweighs any speed the shortcuts buy.

## Implementation

Points 1, 2 and the wait in point 4 are in #757 and the stacked mover fixes. Still open: point 3 (#766), refusing to sort without a snapshot or session in point 4 (#767), and point 5 (#768).

## Considered options

- **Keep check-then-rename and narrow the window.** Rejected: no amount of re-checking closes a TOCTOU race; only an operation that fails atomically does.
- **`link` + `unlink` only.** Standard library and no foreign calls, but it does not cover folders or symlinks, which the mover does move (junctions and links), it leaves the file with two names for a moment, and it fails outright on FAT/exFAT, which are common on USB drives. We kept it as the first fallback rather than the primary path.
- **Drop the `overwrite` policy.** Simplest and safest, and `CONTEXT.md` already lists only `skip` and `rename`. Rejected for now because `app/config.py` accepts `overwrite` and users may rely on it; displacing to a backup keeps it while making it undoable.
- **Size check plus `fsync`, no hash.** Cheaper, but a truncated or corrupted copy of the same size passes. Hashing only runs on the cross-volume path, which is already I/O-bound by the copy.

## Consequences

- The native rename needs per-OS `ctypes` calls and a support check, and every path below it needs its own tests. The tests in #757 cover each fallback.
- On the `link` fallback the file briefly has two names. A crash there leaves a duplicate, never a loss; reconciliation sees two paths with matching hashes and removes the source.
- The lock-based fallback only protects against Sortify's own workers, not other programs writing into the same folder. That residual race is accepted for filesystems without hard links.
- Cross-volume moves get slower by one read of the copy. Same-volume moves cost the same as today.
- Displaced files from `overwrite` use disk space until their session's history is purged.
- `mover.py` and `ledger.py` stop calling `resilient_remove` on user files directly. `scripts/validate_architecture.py` can enforce that only `resilient_file_ops` deletes user files.
- Testing this needs a filesystem seam: inject the file operations into `resilient_file_ops` so tests can fail `link`, `fsync` or the hash at each step. That replaces the `unittest.mock` branches in `resilient_move` and `_process_move_item` rather than adding new ones.
