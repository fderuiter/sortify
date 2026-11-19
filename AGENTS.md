# Sortify agent guide

Local, offline-first Python 3.12 file sorter (`smart-autosorter`). Read `CONTEXT.md` for the domain vocabulary and `docs/adr/` before changing an area an ADR covers.

## Commands

- Install: `uv sync` (add `--all-groups` to match CI static/type checks, and `--extra ml` when the work needs the ML stack: `torch`, `easyocr`, `onnxruntime`, `transformers`, `llama-cpp-python`).
- Offline bundle: `uv run python scripts/prepare_offline.py --cpu` to recompile `offline_bundle/requirements.txt` (`--extra ml --universal` with CPU PyTorch); `uv run --no-project python scripts/prepare_offline.py --cpu --frozen` and `uv run --no-project python scripts/install_offline.py offline-install` to validate the tracked manifest and offline install without overwriting it.
- Fast tests: `uv run pytest -m "not slow"`. One file while iterating: `uv run pytest tests/test_<area>.py -n0`.
- Slow / ML tests: `uv sync --all-groups --extra ml` and `uv run pytest -m "slow"`.
- Bundle build and smoke test: `uv sync --extra ml --group build && uv run python scripts/build.py` (or `uv run python scripts/build.py --lite` without ML weights), then `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring ./dist/smart-autosorter/smart-autosorter --smoke-test` (`dist\smart-autosorter\smart-autosorter.exe --smoke-test` on Windows).
- Lint and format: `uv run ruff check` and `uv run ruff format`.
- Type check: `uv run ty check` and `uv run python scripts/check_ty_warnings.py` (enforces shrink-only per-file warning ceilings in `[tool.sortify.ty-warning-ceilings]`).
- Dependency audit: `uv run deptry .`.
- Vulnerability remediation: `uv run python scripts/remediate_vulnerabilities.py --stage-dir <dir>` (stage validated lockfiles and `remediation_manifest.json` read-only) and `uv run python scripts/remediate_vulnerabilities.py --publish-from <dir>` (publish signed PRs and issue alerts).
- Architecture and import validation: `uv run python scripts/validate_architecture.py` and `uv run python scripts/validate_imports.py`.
- Prompt manifest verification: `uv run python scripts/prompt_manifest.py verify --strict` (generate keypair with `keygen`; re-sign via `PROMPT_MANIFEST_PRIVATE_KEY` with `generate`).
- Hooks: `uv run pre-commit install`; they run ruff, secret detection (gitleaks), workflow linting (actionlint), plus the repo validators in `scripts/`.

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
