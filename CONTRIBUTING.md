# Contributing

Thanks for helping improve Smart AutoSorter AI Pro. This guide covers setting up a checkout, the checks every change has to pass, and how pull requests are reviewed.

## Setup

You need [uv](https://docs.astral.sh/uv/). It reads `.python-version` and installs Python 3.12 if you don't have it.

```bash
git clone https://github.com/fderuiter/sortify.git
cd sortify
uv sync --group dev       # app + dev group (add --group docs or --group build when needed)
uv run pre-commit install # run the hooks on every commit
```

Add `--extra ml` to `uv sync` if you work on OCR, audio transcription, embeddings or LLM naming. Those libraries are imported lazily, so everything else runs without them.

Dependency groups (PEP 735, in `pyproject.toml`):

| Group | Used for |
|---|---|
| `dev` | pytest, ruff, ty, pre-commit and the repo validators |
| `docs` | MkDocs site build |
| `build` | PyInstaller executables (`scripts/build.py`) |

## Everyday commands

```bash
uv run smart-autosorter --demo          # isolated end-to-end run on a generated sample corpus
uv run python sandbox_cli.py analyze -q --json  # isolated sandbox analysis (auto-seeds dataset)
uv run pytest -m "not slow"             # fast suite, parallel by default (pytest-xdist)
uv run pytest -m slow -n 0              # ML, snapshot and integration tests
uv run ruff check . && uv run ruff format .
uv run ty check                         # type check app/, scripts/, tests/, .agents/ and sandbox_cli.py (config in pyproject.toml)
SKIP=no-commit-to-branch uv run pre-commit run --all-files       # every hook offline, including on main
uv run python scripts/validate_config.py --check                 # verify config schema and pyproject.toml bounds read-only
uv run python scripts/generate_docs.py --check --no-build --local-only  # verify generated docs/diagrams offline and read-only
uv run mkdocs serve                     # preview the docs site at http://127.0.0.1:8000
```

Headless runs probe an unavailable OS keyring at most once (within a 2-second timeout) before falling back to local `.key` files, and `--demo` runs inside a temporary workspace without writing to `$HOME`. You can still set `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring` in CI to skip backend discovery upfront.

## What the hooks and CI check

`.pre-commit-config.yaml` runs ruff (lint and format), staged and full-tree secret detection (`gitleaks` and `gitleaks-dir` with `.gitleaks.toml` extending Gitleaks' built-in rules), workflow linting (`actionlint`), ty (type check) plus the repo's own read-only validators in `scripts/` across the full repository:

| Hook | Script | Fails when |
|---|---|---|
| `gitleaks` / `gitleaks-dir` | `gitleaks git --staged --config .gitleaks.toml` / `gitleaks dir . --config .gitleaks.toml` | staged diffs or any non-ignored working-tree file match Gitleaks' built-in credential rules outside the scoped allowlists in `.gitleaks.toml` |
| `validate-docs` | `uv run python scripts/generate_docs.py --check --no-build --local-only` | generated docs (`docs/api_reference.md`, `docs/ui.md`, `docs/admin_guide.md`, `docs/keyboard_shortcuts.md`, `SECURITY.md`, `app/config_schema.json`) are stale, or a handwritten doc links to a missing path |
| `validate-diagram-artifacts` | `uv run python scripts/diagram_toolchain.py --verify` | a Mermaid diagram spec or `.mmd`/`.svg` artifact in `docs/assets/diagrams/` is stale or invalid |
| `validate-signatures` | `uv run python scripts/validate_signatures.py` | a public API or CLI signature changed without updating its baseline in `tests/snapshots/api/`, or an orphaned snapshot exists under `tests/snapshots/` |
| `validate-architecture` | `uv run python scripts/validate_architecture.py` | a centralized helper, `Settings` usage, or architecture diagram inventory drifts across `app/`, `scripts/diagram_schema.py`, or `docs/assets/diagrams/` |
| `deptry` | `uv run deptry .` | a declared dependency is unused, an import comes only in transitively, or runtime code imports a dev-only package (exceptions are in `[tool.deptry]` in `pyproject.toml`) |
| `validate-config-schema` | `uv run python scripts/validate_config.py --check` | `app/config.py` and `app/config_schema.json` disagree, or `pyproject.toml` `hatchling` bounds drift |
| `validate-links` | `uv run python scripts/validate_links.py --local-only` | a URL in a changed `.py` or `.md` file is malformed (`.github/workflows/scheduled-link-audit.yml` runs `scripts/validate_links.py` to check that live external links resolve) |

CI (`.github/workflows/ci.yml`) runs the same checks, a dependency audit and secret scan, then the test suite on Linux, Windows and macOS. The fast suite must keep coverage of `app/` at 70% or above (`--cov-fail-under` in `ci.yml`; on Linux this counts branches too). On Linux, the modules that move, undo and encrypt files must also stay above their own floors in `[tool.sortify.coverage-floors]` in `pyproject.toml`, checked by `scripts/check_coverage_floors.py`. Raise a floor when your tests raise its module. Tests run with a synchronous stand-in for the database write queue; mark a test `@pytest.mark.real_db_worker` to run it against the real asynchronous queue. The aggregate `Required CI` job is the merge gate.

## Updating baselines

Read-only `--check` and `--verify` commands never overwrite tracked files on disk. When you change a baseline on purpose, run the explicit sync command and commit the result so reviewers can see the diff:

- **Configuration schema:** `uv run python scripts/validate_config.py --sync` rewrites `app/config_schema.json` from `app/config.py`.
- **Generated docs:** `uv run python scripts/generate_docs.py` rewrites the generated pages, `SECURITY.md`, and synchronizes `app/config_schema.json` offline without building `site/` (pass `--build` to also build the MkDocs HTML site and `--live-links` to probe external URLs).
- **API and CLI signatures:** `uv run python scripts/validate_signatures.py --update`.
- **TUI snapshots:** `UPDATE_SNAPSHOTS=1 uv run pytest tests/test_visual_snapshots.py -n 0`, then commit `tests/snapshots/tui_svg/`.
- **Diagrams:** edit the spec in `scripts/diagram_schema.py`, then run `uv run python scripts/diagram_toolchain.py build` and commit the `.mmd` and `.svg` files in `docs/assets/diagrams/`. Rendering needs Node and mermaid-cli (`npm install -g @mermaid-js/mermaid-cli`, which also fetches the headless Chrome it uses). Without it the build keeps the existing SVGs and warns that a changed diagram is stale.

## Code conventions

- `.editorconfig` sets UTF-8, LF line endings and 4-space indents (2 for YAML and JSON).
- Ruff enforces formatting, import order and numpy-style docstrings on `app/`.
- Business logic goes in `app/core/`; the terminal UI in `app/ui/` should call into core rather than reimplement it.
- Use the shared helpers in `app/core/path_utils.py`, `app/core/crypto.py` and `app/core/text_utils.py` for paths, packaging checks, keys and secret scrubbing. The [Contributor Guide](docs/contributor.md) lists them.
- New settings go in `app/config.py` and `app/config_schema.json` together.

## Pull requests

1. Branch from `main`; the `no-commit-to-branch` hook blocks commits on `main` itself.
2. Keep each PR to one topic, with tests for behavior changes.
3. Fill in the pull request template.
4. Sensitive paths (workflows, `SECURITY.md`, `PRIVACY.md`, config, crypto, mover, history, session, build tooling and the snapshot baselines) need code-owner review and are never auto-merged. The full list is in [Auto-Merge Policy](docs/auto_merge_policy.md) and `.github/CODEOWNERS`.

## Releasing

Publish a GitHub release (Releases → Draft a new release, with a new `v*` tag). Publishing it runs **Release Binaries** (`.github/workflows/release.yml`), which builds the Windows, Linux and macOS archives, attests their provenance and SBOMs, and attaches them to the release. Pushing a tag on its own does not release anything.

## Reporting bugs and vulnerabilities

Open a GitHub issue for bugs. For security problems follow the reporting steps in [SECURITY.md](SECURITY.md).
