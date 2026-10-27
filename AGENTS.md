# Sortify agent guide

Local, offline-first Python 3.12 file sorter (`smart-autosorter`). Read `CONTEXT.md` for the domain vocabulary and `docs/adr/` before changing an area an ADR covers.

## Commands

- Install: `uv sync` (add `--extra ml` only when the work needs torch/easyocr/transformers).
- Fast tests: `uv run pytest -m "not slow"`. One file while iterating: `uv run pytest tests/test_<area>.py -n0`.
- Lint and format: `uv run ruff check` and `uv run ruff format`.
- Type check: `uv run ty check`.
- Dependency audit: `uv run deptry .`.
- Architecture and import validation: `uv run python scripts/validate_architecture.py` and `uv run python scripts/validate_imports.py`.
- Hooks: `uv run pre-commit install`; they run ruff plus the repo validators in `scripts/`.

## Conventions

- Shared path, packaging, key-lookup and secret-scrubbing helpers live in `app.core.path_utils`, `app.core.crypto` and `app.core.text_utils`; `docs/contributor.md` lists the rules, and `scripts/validate_architecture.py` enforces some of them.
- Inject dependencies so tests run the production path. Production code already holds test-only branches (`PYTEST_CURRENT_TEST`, `unittest.mock` checks); add no new ones.
- Commit on a feature branch; the `no-commit-to-branch` hook blocks `main`.

## Agent skills

Skills live in `.agents/skills/` (Claude Code reads the same folder through the `.claude/skills` symlink). `/ask-matt` routes to the right one.

### Issue tracker

GitHub Issues on `fderuiter/sortify`, via `gh` or the GitHub MCP tools. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default triage roles (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`) plus `bug`/`enhancement` categories. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
