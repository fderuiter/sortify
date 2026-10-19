---
status: accepted (recorded retroactively on 2026-10-19; the rationale is inferred from the code, so correct it if it is wrong)
---

# All database writes go through one writer thread

`DBWorker` (`app/core/db_worker.py`) is the only thread that writes to a session database: callers enqueue write sessions, it runs each inside `BEGIN IMMEDIATE`, and readers use per-thread WAL connections. We chose a single serialised writer over a connection pool because SQLite allows one writer at a time, and extraction and moves run on many worker threads that would otherwise fail with `database is locked` under load.

## Consequences

- Code that writes from a worker thread submits the write to `DBWorker`; opening its own write connection reintroduces lock contention.
- Long-running work does not happen inside a write session, because it blocks every other writer.
