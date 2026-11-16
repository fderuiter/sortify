# Operational Runbook: Transaction Ledger Reconciliation and Recovery

This operational runbook provides authoritative standard operating procedures (SOP) for system administrators and operators managing Sortify (`smart-autosorter`) in headless batch processing and daemon environments.

When headless sorting jobs or background daemon tasks are interrupted—due to system restarts, process termination, hardware faults, or out-of-memory events—uncommitted file transitions may remain in the transaction ledger. This runbook details how to inspect ledger state, execute automated headless reconciliation, resolve conflicts across all terminal transaction statuses, and safely clean up stale lock files.

---

## Table of Contents

1. [Architectural Overview](#architectural-overview)
2. [Transaction Lifecycle and State Transitions](#transaction-lifecycle-and-state-transitions)
3. [CLI Reference and Practical Invocations](#cli-reference-and-practical-invocations)
   - [Common Flags](#common-flags)
   - [Inspecting Incomplete Transactions (`status`)](#inspecting-incomplete-transactions-status)
   - [Executing Automated Reconciliation (`reconcile`)](#executing-automated-reconciliation-reconcile)
   - [Purging Ledger Records (`purge`)](#purging-ledger-records-purge)
   - [Exporting Audit Logs (`export`)](#exporting-audit-logs-export)
4. [Step-by-Step Resolution Paths for Failure Reasons](#step-by-step-resolution-paths-for-failure-reasons)
   - [`RECONCILED_SUCCESS`](#reconciled_success)
   - [`ABORTED`](#aborted)
   - [`FAILED_CONFLICT`](#failed_conflict)
   - [`FAILED_MISSING`](#failed_missing)
5. [Stale Daemon Lock and IPC Socket Recovery](#stale-daemon-lock-and-ipc-socket-recovery)
   - [Lock Mechanics & File Locations](#lock-mechanics--file-locations)
   - [CRITICAL WARNING: Active Process Safety](#critical-warning-active-process-safety)
   - [Safe Verification & Cleanup Procedure](#safe-verification--cleanup-procedure)
6. [Cross-References](#cross-references)

---

## Architectural Overview

Sortify implements a two-phase transaction logging pattern to guarantee file system integrity and prevent data loss during moves:

- **Sidecar Ledger Database**: The ledger is maintained as an independent SQLite database in WAL mode (`transaction_ledger.db`), separate from the main application metadata database. By default, it resides at `~/.autosorter/transaction_ledger.db` (or `$AUTOSORTER_APP_DIR/transaction_ledger.db`).
- **Two-Phase Commit**:
  1. **Intent Logging**: Before moving any physical bytes on disk, Sortify records an entry in the ledger with status `PENDING` via `app/core/ledger.py`.
  2. **Physical Move**: The file is moved on disk. Upon completion of the move, the entry status is updated to `MOVED_PHYSICAL`.
  3. **Metadata Index Update**: The main SQLCipher database is updated with the document's new location. Once updated, the ledger status transitions to `COMPLETED` and the ledger entry is cleaned up.
- **Headless Interruption**: If the process crashes while an entry is in `PENDING` or `MOVED_PHYSICAL`, the next startup or operator reconciliation command evaluates physical disk state against the ledger record to safely roll forward or roll back.

---

## Transaction Lifecycle and State Transitions

Defined in `app/core/ledger.py:L41-L85`, the ledger defines seven statuses across non-terminal and terminal states.

```
                  +-------------------------+
                  |         PENDING         |
                  +-------------------------+
                     /   |       |       \
                    /    |       |        \
                   v     |       |         v
     +----------------+  |       |   +-------------------+
     | MOVED_PHYSICAL |  |       |   |      ABORTED      | (Terminal)
     +----------------+  |       |   +-------------------+
        /    |    \      |       |
       /     |     \     |       |
      v      |      v    v       v
+-----------+|   +--------------------+   +-------------------+
| COMPLETED ||   | RECONCILED_SUCCESS |   |  FAILED_CONFLICT  | (Terminal)
+-----------+|   +--------------------+   +-------------------+
 (Terminal)  |         (Terminal)                   ^
             |                                      |
             +--------------------------------------+
             |
             v
   +-------------------+
   |  FAILED_MISSING   | (Terminal)
   +-------------------+
```

### Status Definitions

| Status | Category | Description |
|---|---|---|
| `PENDING` | Non-Terminal | Move intent logged to sidecar ledger prior to beginning physical disk operation. |
| `MOVED_PHYSICAL` | Non-Terminal | File was physically relocated on disk, but main database record was not yet committed. |
| `COMPLETED` | Terminal | Standard completion: file physically relocated and application database record committed. |
| `RECONCILED_SUCCESS` | Terminal | Automated reconciliation detected the destination file intact on disk and updated database records. |
| `ABORTED` | Terminal | Move cancelled prior to physical transfer, or identical duplicate destination removed and source preserved. |
| `FAILED_CONFLICT` | Terminal | Reconciliation encountered both source and destination files with differing contents; both preserved to avoid loss. |
| `FAILED_MISSING` | Terminal | Reconciliation found that neither the source file nor destination file exists on disk. |

### Allowed State Transitions

Transitions are strictly validated by `ALLOWED_TRANSITIONS` in `app/core/ledger.py`:

- **From `PENDING`**: Allowed to transition to `MOVED_PHYSICAL`, `COMPLETED`, `RECONCILED_SUCCESS`, `ABORTED`, `FAILED_CONFLICT`, or `FAILED_MISSING`.
- **From `MOVED_PHYSICAL`**: Allowed to transition to `COMPLETED`, `RECONCILED_SUCCESS`, `ABORTED`, `FAILED_CONFLICT`, or `FAILED_MISSING`.
- **Terminal States**: `COMPLETED`, `RECONCILED_SUCCESS`, `ABORTED`, `FAILED_CONFLICT`, and `FAILED_MISSING` allow **no further transitions** (`frozenset()`). Once an entry reaches a terminal state, its record is immutable until purged.

---

## CLI Reference and Practical Invocations

The `smart-autosorter ledger` command group is defined in `app/cli/ledger_cli.py`. Operators use these subcommands for non-interactive maintenance and batch session recovery.

### Common Flags

These flags apply to `smart-autosorter ledger` and all its subcommands:

- `--ledger-db PATH`: Path to a custom sidecar transaction ledger database file. Defaults to `~/.autosorter/transaction_ledger.db`.
- `-q`, `--quiet`: Suppress human-readable informational banners and status summaries.
- `--no-color`: Disable ANSI color escape codes (recommended for cron and headless log collection).
- `--json`: Output response payloads in structured, machine-parseable JSON format on standard output.

---

### Inspecting Incomplete Transactions (`status`)

Inspect pending, interrupted, or failed transaction entries currently recorded in the ledger.

#### Flags for `status`
- `--session-id SESSION_ID`: Filter results to entries associated with a specific batch session identifier.
- `--failed`: Filter exclusively for failed and aborted transaction records (`FAILED_CONFLICT`, `FAILED_MISSING`, `ABORTED`).
- `--aborted`: Filter exclusively for aborted transaction records (`ABORTED`).
- `--json`: Return structured JSON array of pending entries.
- `--ledger-db PATH`: Target an alternate ledger database file.
- `-q`, `--quiet`: Omit the header summary line.
- `--no-color`: Suppress ANSI color codes.

#### Practical Examples

1. **Check all incomplete, pending, or failed entries across all sessions:**
   ```bash
   smart-autosorter ledger status
   ```
   *Example Output:*
   ```text
   Transaction Ledger Status: 2 pending, incomplete, or failed entry(ies).
     [PENDING] sess_9a8b1c:docs/invoices/inv_101.pdf | Source: /data/staging/inv_101.pdf -> Dest: /data/sorted/Finance/inv_101.pdf
     [FAILED_CONFLICT] sess_9a8b1c:docs/reports/annual.pdf | Source: /data/staging/annual.pdf -> Dest: /data/sorted/Reports/annual.pdf | Reason: Destination file exists and content differs from source; both files preserved.
   ```

2. **Filter by specific session ID:**
   ```bash
   smart-autosorter ledger status --session-id "sess_9a8b1c"
   ```

3. **Filter exclusively for failed or conflicted records:**
   ```bash
   smart-autosorter ledger status --failed
   ```

4. **Structured JSON output for pipeline automation:**
   ```bash
   smart-autosorter ledger status --json
   ```
   *Example JSON Output:*
   ```json
   {
     "status": "success",
     "count": 1,
     "pending_entries": [
       {
         "entry_id": "sess_9a8b1c:docs/reports/annual.pdf",
         "session_id": "sess_9a8b1c",
         "base_dir": "/data/staging",
         "source_path": "/data/staging/annual.pdf",
         "dest_path": "/data/sorted/Reports/annual.pdf",
         "source_rel_path": "docs/reports/annual.pdf",
         "dest_rel_path": "Reports/annual.pdf",
         "current_dest": "Reports/annual.pdf",
         "file_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
         "status": "FAILED_CONFLICT",
         "created_at": 1731715200.0,
         "updated_at": 1731715205.0,
         "failure_reason": "Destination file exists and content differs from source; both files preserved."
       }
     ]
   }
   ```

---

### Executing Automated Reconciliation (`reconcile`)

Reconciliation scans all non-terminal transactions (`PENDING` and `MOVED_PHYSICAL`), queries actual physical filesystem state, aligns database records, updates terminal statuses, and purges completed entries.

#### Flags for `reconcile`
- `--ledger-db PATH`: Target a custom ledger database file.
- `--json`: Output result as a JSON dictionary `{"status": "success", "reconciled_count": <int>}`.
- `-q`, `--quiet`: Suppress confirmation prints to stderr.
- `--no-color`: Disable color output.

#### Practical Examples

1. **Standard headless reconciliation:**
   ```bash
   smart-autosorter ledger reconcile
   ```
   *Example Output:*
   ```text
   Transaction ledger reconciliation completed. Reconciled 2 entry(ies).
   ```

2. **Headless cron execution with JSON telemetry:**
   ```bash
   smart-autosorter ledger reconcile --json --quiet
   ```
   *Example JSON Output:*
   ```json
   {
     "status": "success",
     "reconciled_count": 2
   }
   ```

---

### Purging Ledger Records (`purge`)

Remove finalized, completed, aborted, or failed transaction entries from the sidecar database once operational review or audits are complete.

#### Flags for `purge`
- `--session-id SESSION_ID`: Limit purge to records belonging to a specific session ID.
- `--completed`: Purge all entries with status `COMPLETED` and `RECONCILED_SUCCESS`.
- `--failed`: Purge all entries with status `FAILED_CONFLICT`, `FAILED_MISSING`, and `ABORTED`.
- `--aborted`: Purge specifically entries with status `ABORTED`.
- `--force`: Bypass the interactive confirmation prompt (`Are you sure you want to purge transaction ledger records? [y/N]`). **Mandatory** for scripts, CI, and non-interactive environments without a TTY.
- `--json`: Output JSON confirmation payload.
- `--ledger-db PATH`: Target custom ledger database.
- `-q`, `--quiet`: Suppress informational messages.
- `--no-color`: Disable color output.

#### Practical Examples

1. **Purge all completed records across all sessions:**
   ```bash
   smart-autosorter ledger purge --completed
   ```

2. **Non-interactive batch script purge with `--force`:**
   ```bash
   smart-autosorter ledger purge --session-id "sess_9a8b1c" --failed --force --json
   ```
   *Example JSON Output:*
   ```json
   {
     "status": "success",
     "message": "Transaction ledger records purged successfully.",
     "session_id": "sess_9a8b1c",
     "completed_only": false,
     "failed_only": true,
     "aborted_only": false
   }
   ```

3. **Purge all aborted transactions without touching conflicted or missing records:**
   ```bash
   smart-autosorter ledger purge --aborted --force
   ```

---

### Exporting Audit Logs (`export`)

Export session history and transaction audit logs from the application database for compliance, auditing, or forensic analysis.

#### Flags for `export`
- `--session-id SESSION_ID`: Export records for a specific session ID (exports all historical records if omitted).
- `-o`, `--output PATH`: Specify output file path (defaults to `audit_log_<session_id or 'all'>.csv` or `.json`).
- `-f`, `--format {csv,json}`: Explicit export format.
- `--csv`: Shorthand convenience flag to export in CSV format.
- `--json`: Output execution summary in JSON format to stdout.
- `--ledger-db PATH`: Target custom ledger database.
- `-q`, `--quiet`: Suppress informational prints to stderr.
- `--no-color`: Disable color output.

#### Practical Examples

1. **Export session audit trail to CSV:**
   ```bash
   smart-autosorter ledger export --session-id "sess_9a8b1c" --csv -o /var/log/sortify/audit_sess_9a8b1c.csv
   ```
   *Example Output:*
   ```text
   Audit log exported (CSV) to '/var/log/sortify/audit_sess_9a8b1c.csv'. 48 record(s) written.
   ```

2. **Export all sessions to JSON formatted audit report:**
   ```bash
   smart-autosorter ledger export --format json -o /var/log/sortify/full_audit.json --json
   ```

---

## Step-by-Step Resolution Paths for Failure Reasons

When reconciliation executes, transactions evaluate to one of four terminal outcomes. Follow these procedures to resolve each case safely.

```
                    Reconciliation Evaluation
                                |
             +------------------+------------------+
             |                                     |
    Source missing?                       Source exists?
             |                                     |
     +-------+-------+                     +-------+-------+
     |               |                     |               |
Destination     Destination           Destination     Destination
  exists?         missing?              missing?        exists?
     |               |                     |               |
     v               v                     v               v
RECONCILED_     FAILED_MISSING          ABORTED        Check Content
 SUCCESS     (Files disappeared)    (Move aborted;        Match?
(Roll forward                         Source kept)         |
  database)                                        +-------+-------+
                                                   |               |
                                              Identical?       Different?
                                                   |               |
                                                   v               v
                                                ABORTED     FAILED_CONFLICT
                                              (Duplicate    (Both files kept;
                                               removed)     operator triage)
```

---

### `RECONCILED_SUCCESS`

- **Condition**: Physical move completed (`MOVED_PHYSICAL` or destination file exists on disk while source file is gone).
- **Automated Action**: Reconciliation automatically rolls forward the main application database record (`update_document_path` and `set_user_verified_target`) to point to the destination path.
- **Operator Action Required**: None. The move succeeded on disk, and the database now reflects the new location. Completed entries are purged automatically by `reconcile`.

---

### `ABORTED`

An aborted state occurs under two distinct scenarios:

#### Scenario A: Move aborted prior to physical move
- **Reason String**: `"Move aborted prior to physical file move; source file preserved at origin."`
- **Condition**: Status was `PENDING`. Source file exists at its original path; destination file does not exist.
- **Cause**: Daemon process was terminated before filesystem operations commenced.
- **Resolution Path**:
  1. Confirm source file presence:
     ```bash
     ls -la "/data/staging/file.pdf"
     ```
  2. The database retains the source file path. The source file is completely intact.
  3. Re-queue or re-run sorting on the source directory:
     ```bash
     smart-autosorter sort /data/staging
     ```
  4. Purge the aborted ledger entry:
     ```bash
     smart-autosorter ledger purge --aborted --force
     ```

#### Scenario B: Duplicate destination safely removed
- **Reason String**: `"Incomplete move left duplicate destination; duplicate removed and source preserved."`
- **Condition**: Both source and destination files existed, and `same_file_content` confirmed they were byte-identical (or hard-linked).
- **Automated Action**: Reconciliation safely deleted the duplicate file at destination and preserved the source file at origin. Database was kept pointing to the source.
- **Resolution Path**: Verify source file integrity and re-sort when ready.

---

### `FAILED_CONFLICT`

A conflict failure indicates that Sortify prevented silent data overwrite or data loss.

#### Scenario A: Destination file exists with differing content
- **Reason String**: `"Destination file exists and content differs from source; both files preserved."`
- **Condition**: Both source and destination files exist on disk, but their byte contents or checksums differ.
- **Cause**: A preexisting or independently generated file exists at the target destination path with identical naming.
- **Resolution Path**:
  1. Identify the conflicting paths from ledger status:
     ```bash
     smart-autosorter ledger status --failed --json
     ```
  2. Inspect and compare both files manually using checksums or diff tools:
     ```bash
     sha256sum "/data/staging/report.pdf" "/data/sorted/Reports/report.pdf"
     ```
  3. Determine the correct canonical version:
     - **If the destination file is valid and should not be replaced**: Rename the source file or move it to a dedicated manual triage folder:
       ```bash
       mv "/data/staging/report.pdf" "/data/staging/report_v2.pdf"
       ```
     - **If the source file should overwrite the destination file**: Back up or remove the conflicting destination file manually:
       ```bash
       mv "/data/sorted/Reports/report.pdf" "/data/sorted/Reports/report_archive.pdf"
       ```
  4. Re-run reconciliation to update the ledger:
     ```bash
     smart-autosorter ledger reconcile
     ```
  5. Once resolved, purge failed entries from the ledger:
     ```bash
     smart-autosorter ledger purge --failed --force
     ```

#### Scenario B: Duplicate destination could not be removed
- **Reason String**: `"Reconciliation conflict for <entry_id>: duplicate destination could not be removed: <error>"`
- **Condition**: Destination was identical to source, but filesystem permissions or file locks prevented deleting the duplicate destination.
- **Resolution Path**:
  1. Inspect file permissions and open handles on the destination file:
     ```bash
     lsof "/data/sorted/Reports/report.pdf"
     ls -la "/data/sorted/Reports/report.pdf"
     ```
  2. Release any locking processes or adjust permissions (`chmod +w /data/sorted/Reports/report.pdf`).
  3. Remove the duplicate destination file manually.
  4. Run `smart-autosorter ledger reconcile` again.

---

### `FAILED_MISSING`

- **Reason String**: `"Neither source nor destination file exists."`
- **Condition**: The ledger logged intent to move a file, but neither the source path nor the destination path exists on the filesystem.
- **Cause**:
  - Monitored network share, NAS, or removable volume was unmounted.
  - An external script or operator deleted or moved the file out-of-band while Sortify was running.
- **Resolution Path**:
  1. Verify mount points and volume availability:
     ```bash
     df -h
     mount | grep -i "/data"
     ```
  2. If the volume was detached, remount the volume and re-run reconciliation:
     ```bash
     mount -a
     smart-autosorter ledger reconcile
     ```
  3. If the file was intentionally deleted out-of-band:
     - Export an audit record of the event:
       ```bash
       smart-autosorter ledger export --csv -o /var/log/sortify/missing_files_audit.csv
       ```
     - Purge the missing record from the ledger:
       ```bash
       smart-autosorter ledger purge --failed --force
       ```

---

## Stale Daemon Lock and IPC Socket Recovery

Background directory watching daemons (`smart-autosorter daemon start`) manage an IPC server and write lock files to prevent duplicate daemon processes on the same directory tree.

### Lock Mechanics & File Locations

Referencing `app/core/ipc.py:L83-L110`:
- **Lock Files**:
  - `.autosorter.pid`: Written to the base directory of the watched folder. Contains a JSON payload with `pid`, `socket_path`, `auth_token`, and creation timestamp.
  - `.autosorter.sock`: Unix domain socket file created in the base directory (on Linux/macOS) or a loopback TCP listener on `127.0.0.1` (on Windows).
- **Stale Lock Contention**: Occurs when a daemon process was abruptly halted (e.g., `kill -9`, power outage, kernel OOM) and was unable to execute shutdown hooks to unlink `.autosorter.pid` and `.autosorter.sock`.

---

### CRITICAL WARNING: Active Process Safety

> **DANGER: NEVER UNLINK LOCK FILES FOR AN ACTIVE PROCESS**
>
> Deleting `.autosorter.pid` or `.autosorter.sock` while a daemon process is actively running will break process coordination and may result in:
> - Multiple concurrent sorting daemons operating on identical directories (**split-brain**).
> - Concurrent file moves causing collision errors and data corruption.
> - Broken IPC telemetry and client communication failure.
>
> **Always verify process liveness before removing any lock or socket files.**

---

### Safe Verification & Cleanup Procedure

Follow this strict three-step procedure to resolve lock contention safely:

#### Step 1: Inspect the PID File
Locate the `.autosorter.pid` file in the base watched directory and read the recorded process ID:

```bash
cat /data/staging/.autosorter.pid
```

*Example Output:*
```json
{"pid": 48219, "socket_path": "/data/staging/.autosorter.sock", "auth_token": "a1b2c3d4..."}
```

#### Step 2: Verify Process Liveness
Verify whether the process ID is actively executing on the system.

**On Linux / macOS:**
```bash
# Check if process exists and is active
kill -0 48219 2>/dev/null && echo "ACTIVE PROCESS DETECTED" || echo "PROCESS NOT RUNNING (STALE)"

# Inspect process details if alive
ps -fp 48219
```

**On Windows (PowerShell / Command Prompt):**
```powershell
Get-Process -Id 48219 -ErrorAction SilentlyContinue
# Or via cmd.exe:
tasklist /FI "PID eq 48219"
```

- **If the process IS alive (`ACTIVE PROCESS DETECTED`)**:
  - **DO NOT delete the lock files.**
  - If you need to stop the daemon, send a graceful shutdown signal:
    ```bash
    smart-autosorter daemon stop --target /data/staging
    # Or send SIGTERM:
    kill -15 48219
    ```
  - Wait for the process to exit cleanly and remove its own lock files.

- **If the process is NOT running (`PROCESS NOT RUNNING (STALE)`)**:
  - The process died abruptly; the lock files are stale and safe to clean. Proceed to Step 3.

#### Step 3: Remove Stale Lock Files
Once process termination is verified:

```bash
# Remove socket file first if present
rm -f /data/staging/.autosorter.sock

# Remove pid file
rm -f /data/staging/.autosorter.pid
```

*(Note: Starting the daemon via `smart-autosorter daemon start /data/staging` also checks process liveness automatically using `is_process_alive()` in `app/core/ipc.py:L20-L53` and will automatically clean stale lock files if the previous process is dead).*

---

## Cross-References

- **Interactive Session Recovery Guide**: [Troubleshooting Guide](../troubleshooting.md#session-recovery)
- **Configuration & Maintenance Reference**: [Administrator Guide](../admin_guide.md)
- **Core Ledger Implementation**: `app/core/ledger.py`
- **CLI Ledger Command Implementation**: `app/cli/ledger_cli.py`
- **IPC Server & Lock Management**: `app/core/ipc.py`
