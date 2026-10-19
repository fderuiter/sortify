---
name: run-sortify
description: Run, start, launch, drive, screenshot or smoke-test Sortify (Smart AutoSorter AI Pro) - the Textual TUI and the smart-autosorter CLI (sort, scan, undo, dry-run). Use to see a UI or sorting change working in the real app, not just in pytest.
---

# Run Sortify

Sortify is a Python 3.12 file organizer with two user surfaces: the
`smart-autosorter` CLI (headless `sort`/`scan`/`undo`) and a Textual TUI
(`app/ui/tui.py`). The agent path for the TUI is
`.agents/skills/run-sortify/driver.py`: it boots `AutoSorterTUI` under
Textual's Pilot (no TTY), in a throwaway app dir + `HOME`, against a
generated 8-file corpus, and takes key presses / screenshots / tree dumps
as commands. The CLI is driven directly.

All paths are relative to the repo root. Verified on Linux (Ubuntu
container), Python 3.12.13 via uv, Textual 8.2.8.

## Prerequisites

`uv` on PATH. Nothing from apt was needed. PNG screenshots use the
pre-installed Chromium at `/opt/pw-browsers/chromium-1194/chrome-linux/chrome`
(falls back to `chromium` on PATH; without either you still get the SVG).

## Build

```bash
uv sync --frozen --all-groups   # ~15s; Python 3.12 is fetched by uv per .python-version
```

Use `--all-groups`, as CI does: a plain `uv sync` leaves out test plugins,
and about 60 async tests then fail with "async def functions are not
natively supported". The `[ml]` extra (torch, easyocr, llama-cpp...) is not needed: ML imports
are lazy and TF-IDF clustering works without it.

## Run the TUI (agent path)

One-shot, commands as `-c` flags (stderr is keyring noise, drop it):

```bash
uv run python .agents/skills/run-sortify/driver.py \
  -c "press ctrl+s" -c "idle" -c "tree" -c "press down down" -c "ss planned" \
  -c "press ctrl+e" -c "idle" -c "screen" -c "ss executed" -c "files" 2>/dev/null
```

That is the full user flow: scan (Ctrl+S) and plan the corpus into `Budget-Balance/` and
`Cluster-Backend/`, show it in the tree, execute the moves (Ctrl+E), land
on the `AuditReportModal` with 8 `[PASS]` rows, and list the moved files.
Screenshots go to `.sortify-driver/<name>.svg` + `.png` (gitignored).
**Look at the PNG** with the Read tool.

Commands (`--script FILE` for a file of them, or pipe on stdin for a REPL):

| Command | Does |
|---|---|
| `press KEY...` | Textual key names: `ctrl+s`, `ctrl+e`, `ctrl+o`, `down`, `enter`, `escape`, `G` |
| `type TEXT` | type literal characters into the focused widget |
| `click SELECTOR` | click by CSS selector, e.g. `#btn-execute` |
| `idle [SECS]` | wait for all workers (scan/execute run in threads) |
| `wait SECS` | let the app run |
| `ss NAME` | screenshot to `.sortify-driver/NAME.{svg,png}` |
| `tree` / `plan` / `screen` / `files` | dump plan tree, `app.plan` JSON, active screen + focus + status bar, target dir |
| `eval PY` | eval/exec with `app`, `pilot`, `target` in scope |

Options: `--dir PATH` (sort your own folder - it **moves files** on
`ctrl+e`), `--width/--height` (default 120x36), `--out DIR`,
`--keep-state` (reuse the caller's `AUTOSORTER_APP_DIR`/`HOME`).

REPL mode under tmux, for poking around interactively:

```bash
tmux new-session -d -s drv -x 200 -y 50 "uv run python .agents/skills/run-sortify/driver.py 2>/dev/null"
until tmux capture-pane -t drv -p | grep -q "sortify>"; do sleep 1; done
tmux send-keys -t drv "press ctrl+f" Enter "type fin 2" Enter "eval app.query_one('#tree-search-input').value" Enter
tmux send-keys -t drv "press ctrl+o" Enter "screen" Enter "ss settings" Enter
until tmux capture-pane -t drv -p | grep -q "saved"; do sleep 1; done
tmux capture-pane -t drv -p | grep -v '^$' | tail
tmux send-keys -t drv "quit" Enter
```

## Run the real TUI binary in tmux

Use this when the bug might be in startup/argument handling rather than in
`AutoSorterTUI` itself. Ctrl+S should show `Budget-Balance` and
`Cluster-Backend` in the tree; `C-e` then moves the files:

```bash
D=$(mktemp -d); mkdir $D/inbox; for i in 1 2 3 4; do
  echo "Quarterly revenue budget invoice tax ledger profit expense accounting balance sheet fiscal report $i" > $D/inbox/fin$i.txt
  echo "Python kubernetes docker server deployment API database backend microservice cluster code $i" > $D/inbox/tech$i.txt; done
tmux new-session -d -s sortify -x 120 -y 36 \
  "AUTOSORTER_APP_DIR=$D/app uv run smart-autosorter sort $D/inbox --tui --skip-wizard --accept-ai-consent; sleep 600"
sleep 8; tmux send-keys -t sortify C-s; sleep 10
tmux capture-pane -t sortify -p | head -25
tmux kill-session -t sortify
```

## Run the CLI

Reuses the `$D/inbox` corpus from the block above:

```bash
export AUTOSORTER_APP_DIR=$D/app        # always isolate state
uv run smart-autosorter sort $D/inbox --dry-run --json --accept-ai-consent --non-interactive 2>/dev/null > $D/plan.json
uv run smart-autosorter sort $D/inbox --accept-ai-consent --non-interactive   # really moves files
uv run smart-autosorter undo --list
uv run smart-autosorter undo --latest   # "Rollback completed successfully for session ..."
```

The `--json` output has keys `status, dry_run, target_directory,
destination_directory, plan`; stdout is clean JSON when stderr is
redirected. A sort of the 8-file corpus takes ~13s.

## Test

```bash
uv run pytest -m "not slow" -n auto tests/ -q  # what CI runs; ~5 min
uv run pytest tests/test_visual_snapshots.py   # TUI SVG snapshots (slow marker)
```

## Gotchas

- **Clustering spawns a subprocess, which breaks inside Textual** unless
  multiprocessing's resource tracker is started before Textual replaces
  `sys.stderr`: `ValueError: bad value(s) in fds_to_keep` in
  `autosorter.log`, a `{}` plan, and an "Empty Workspace" tree while the
  status bar says "Analyzed 8 files". `run_tui` starts the tracker (#792);
  the driver does the same before `run_test()`. If you see that symptom,
  grep `autosorter.log` for `fds_to_keep`.
- Ctrl+S runs `MetadataPass` first; files it routes by rule (keywords,
  extensions) skip text extraction, and the rest go through
  `build_corpus_generator` + `partial_fit` (`run_scan_worker` in
  `app/ui/tui.py`), the same as CLI `sort`.
- **Never redirect stderr of the real TUI** (`2>file`): Textual draws to
  stderr, so the whole UI goes into the file and tmux shows raw escapes.
- **State leaks across runs.** Sessions live under `AUTOSORTER_APP_DIR`
  (default `~/.autosorter`); key files go to `~/.sortify/keys` regardless.
  A crashed or killed sort shows up as "Interrupted Session Detected" on
  the next TUI start. The driver isolates both by
  default; for the CLI set `AUTOSORTER_APP_DIR` (and `HOME`) to a temp dir.
- Textual SVGs pull Fira Code from cdnjs, which the container proxy
  blocks; the PNG renders with a fallback monospace font. Proxy
  "connect_rejected" notices for cdnjs/google during `ss` are that, harmless.
- **No OS keyring in the container**: every DB open logs "Keyring lookup
  failed ... No recommended backend" to stderr. Harmless; keys fall back to
  files. That is why the commands above use `2>/dev/null`.
- **`undo` leaves the empty generated folders behind**; only files move back.
- After Ctrl+E the TUI rescans the now-sorted folder, so the status bar
  shows the scan message, not the move result. Check `files` or the modal.

## Troubleshooting

| Symptom | Fix |
|---|---|
| tmux pane full of `[?1049h[48;5;17m...` | you redirected stderr of the TUI; remove `2>` |
| Tree shows "Empty Workspace" after `press ctrl+s` + `idle` | `fds_to_keep` in `autosorter.log` (resource tracker not pre-started), or fewer than 3 text files in `--dir` |
| ~60 async tests fail with "async def functions are not natively supported" | `uv sync --frozen --all-groups` |
| `ss` prints "png conversion failed" | no Chromium found; open the `.svg` instead |
| Startup shows "Interrupted Session Detected" | stale `AUTOSORTER_APP_DIR`; use a fresh one (driver default) |
