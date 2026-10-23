# CLI Testing Patterns

When applying TDD to CLI commands, the standard Red-Green-Refactor loop applies but the testing strategies require CLI-specific adaptations. Sortify's CLI is `argparse` in `app/main.py` (`smart-autosorter`), and its interactive UI is a Textual app.

## The Testing Pyramid for CLIs

| Level | Scope | Method | Goal |
|---|---|---|---|
| **Unit** (most tests) | Individual functions, services, commands | Call the function with `tmp_path` and fakes at system boundaries | Verify logic and error handling |
| **Integration** (fewer) | The entry point | Call `main()` with a patched `sys.argv`, or spawn `uv run smart-autosorter` and capture stdout/stderr | Verify the "glue" code and startup |
| **Interactive** (fewest) | The Textual TUI | `async with app.run_test() as pilot:` then `await pilot.press(...)` | Ensure screens mount, keys work, nothing crashes |

## Behavior-Centric Test Naming

Avoid naming tests after implementation details. This couples tests to the framework.

```python
# Bad: coupled to argparse internals
def test_subparsers_has_undo_action(): ...


# Good: describes behavior, survives framework swaps
def test_undo_latest_restores_last_session(): ...
def test_sort_json_flag_prints_machine_readable_plan(): ...
```

If you rename an internal function and your tests break, the tests are named wrong.

## Framework Migration Pattern

When migrating from one CLI framework to another (e.g., argparse → Typer):

### Step 1: Lock current behavior

Write tests that assert the current output of every command. These tests should pass immediately against the existing implementation.

```python
import json, subprocess


def run_cli(*args, cwd):
    return subprocess.run(
        ["uv", "run", "smart-autosorter", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_scan_json_lists_planned_moves(tmp_path, sample_corpus):
    proc = run_cli("scan", str(sample_corpus), "--json", cwd=tmp_path)
    assert proc.returncode == 0
    assert "moves" in json.loads(proc.stdout)
```

### Step 2: Swap the implementation

Replace the framework code. The tests will go Red.

### Step 3: Fix until Green

Update the new implementation until all locked tests pass. Do not modify the tests — they represent the required behavior.

## CLI-Specific Gotchas

- **Subprocess timeouts:** Real CLI commands are slow (model loading, SQLCipher setup). Mock heavy operations in unit tests and mark real subprocess tests `@pytest.mark.slow`.
- **Environment leakage:** Settings read env vars, `.env` and `~/.autosorter/settings.json`. Use `monkeypatch.setenv` / `monkeypatch.delenv` and point `AUTOSORTER_APP_DIR` at `tmp_path` so tests never touch the real profile.
- **Stdout assertions are fragile:** Don't assert exact stdout strings (they break when formatting changes). Assert `--json` output or check for key fragments with `in`.
- **Exit code semantics:** Test both stdout/stderr *and* exit codes. A command can print correct output but exit with code 1, or vice versa.
- **Parallel runs:** pytest runs with `-n auto --dist=loadgroup`. Tests that share a resource (a port, a global registry) need `@pytest.mark.xdist_group("name")`.
