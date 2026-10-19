---
status: accepted (2026-10-19)
---

# One sort pipeline, built in one place, with layers enforced in CI

A sort runs through one `SortPipeline` service in `app/core`, and every front end calls it instead of wiring the steps itself. It has three methods: `plan(folders, options) -> Plan`, `execute(plan) -> SortResult` and `undo(session_id) -> SortResult`. The pipeline owns the order of steps (discover, extract, route and cluster, verify, move), the session, and a per-folder lock file, so two sorts can never run on one folder. `SortResult` lists what was moved, skipped, quarantined and failed, with reasons, and every front end renders it the same way.

One composition root builds the store, key provider, settings, plugin registry and pipeline. The CLI, TUI, Drop Zone, daemon and demo receive a pipeline and never construct a `Database`, `DBWorker` or history manager themselves. CLI flags are an in-memory overlay passed to `plan`; only `config --set` and the settings screen save settings. Tests build the same graph with fakes.

Imports point one way: `app.main`, `app.cli` and `app.ui` may import `app.core`, never the reverse, and plugins reach core only through the registry. `scripts/validate_architecture.py` fails a PR when `app.core` imports `app.main`, `app.cli` or `app.ui`, or when a front end imports a store module (`db`, `db_worker`, `history`, `ledger`) directly (#707).

We chose this because the scan, extract, fit, plan and move sequence is copied about ten times across `main.py`, `tui.py`, `demo.py`, `daemon.py` and `session.py` (#708), and each copy wires its own database and history paths (#709). The copies drift: the TUI's Ctrl+S scan never extracts text (#754), the daemon runs blocking sort work on its event loop (#717), Drop Zone and the daemon skip the plan preview the CLI shows, and one-off CLI flags get saved permanently (#735). Since bots write most changes and copy whatever the code already does, the boundary has to be a check, not a convention.

## Considered options

- **A shared helper function per step, called by each front end.** Less churn, but every front end still decides the order and the error handling, which is exactly what has drifted.
- **One `run_sort()` function with flags.** Simpler than a service, but the TUI needs to show the plan and ask before moving, and the daemon needs to queue work. Separate `plan` and `execute` serve both, and `run_sort` is just `execute(plan(...))`.
- **Dependency-injection framework.** Not needed: one hand-written function that builds the graph is enough at this size and is easier for an agent to read.

## Consequences

- Drop Zone and the daemon get the same preview and simulation as the CLI, and the daemon becomes a thin asyncio front end that queues work for the pipeline (#717).
- The production `unittest.mock` and `PYTEST_CURRENT_TEST` branches (#720) can go, because tests inject fakes through the composition root instead.
- Plugins add routers, strategies and quarantine hooks through one registry, so core stops naming `clinical_compliance` (#723).
- Splitting `tui.py` (#710) gets easier: screens call the pipeline instead of holding sort logic.
- Front ends lose the freedom to reorder steps. A new need, such as a scan-only mode, becomes a pipeline option rather than another copy.
