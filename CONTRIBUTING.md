# Contributing

Thanks for helping improve Smart AutoSorter AI Pro. This guide covers setting up a checkout, the checks every change has to pass, and how pull requests are reviewed.

## Setup

You need [uv](https://docs.astral.sh/uv/). It reads `.python-version` and installs Python 3.12 if you don't have it.

```bash
git clone https://github.com/fderuiter/sortify.git
cd sortify
uv sync --all-groups      # app + dev, docs, notebooks and build groups
uv run pre-commit install # run the hooks on every commit
```

Add `--extra ml` to `uv sync` if you work on OCR, audio transcription, embeddings or LLM naming. Those libraries are imported lazily, so everything else runs without them.

Dependency groups (PEP 735, in `pyproject.toml`):

| Group | Used for |
|---|---|
| `dev` | pytest, ruff, mypy, pre-commit and the repo validators |
| `docs` | MkDocs site build |
| `build` | PyInstaller executables (`scripts/build.py`) |
| `notebooks` | Jupyter |

## Everyday commands

```bash
uv run smart-autosorter --demo          # end-to-end run on a generated sample corpus
uv run pytest -m "not slow"             # fast suite, parallel by default (pytest-xdist)
uv run pytest -m slow -n 0              # ML, snapshot and integration tests
uv run ruff check . && uv run ruff format .
uv run mypy app/
uv run pre-commit run --all-files       # every hook, same as CI's static checks
uv run docs --check                     # regenerate docs, audit links, build the site
uv run mkdocs serve                     # preview the docs site at http://127.0.0.1:8000
```

Set `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring` when running tests on a machine without a usable keyring (CI does this).

## What the hooks and CI check

`.pre-commit-config.yaml` runs ruff (lint and format) plus the repo's own validators in `scripts/`:

| Hook | Script | Fails when |
|---|---|---|
| `validate-docs` | `uv run docs --check --no-build` | generated docs (`docs/api_reference.md`, `docs/ui.md`, `docs/admin_guide.md`, `SECURITY.md`) are stale, or a handwritten doc links to a missing path |
| `validate-diagram-artifacts` | `scripts/diagram_toolchain.py` | a Mermaid diagram spec doesn't compile |
| `validate-signatures` | `scripts/validate_signatures.py` | a public API or CLI signature changed without updating its baseline in `tests/snapshots/api/` |
| `validate-architecture` | `scripts/validate_architecture.py` | a centralized helper (path validation, packaging checks, key lookup) is duplicated instead of reused |
| `validate-imports` | `scripts/validate_imports.py` | `app/` imports a package that isn't a declared dependency |
| `validate-config-schema` | `scripts/validate_config.py` | `app/config.py` and `app/config_schema.json` disagree |
| `validate-links` | `scripts/validate_links.py --local-only` | a URL in a changed file is malformed (CI also checks that external links resolve) |

CI (`.github/workflows/ci.yml`) runs the same checks, mypy, a dependency audit and secret scan, then the test suite on Linux, Windows and macOS. The aggregate `Required CI` job is the merge gate.

## Updating baselines

Some checks compare against committed files. When you change them on purpose, regenerate and commit the result so reviewers can see the diff:

- **Generated docs:** `uv run docs` rewrites the generated pages and `SECURITY.md`.
- **API and CLI signatures:** `uv run python scripts/validate_signatures.py --update`.
- **TUI snapshots:** `UPDATE_SNAPSHOTS=1 uv run pytest tests/test_visual_snapshots.py -n 0`, then commit `tests/snapshots/tui_svg/`.
- **Diagrams:** edit the spec in `app/core/diagram_schema.py`, then run `uv run python scripts/diagram_toolchain.py build` and commit the `.mmd` and `.svg` files in `docs/assets/diagrams/`. Rendering needs Node and mermaid-cli (`npm install -g @mermaid-js/mermaid-cli`, which also fetches the headless Chrome it uses). Without it the build keeps the existing SVGs and warns that a changed diagram is stale.

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

## Reporting bugs and vulnerabilities

Open a GitHub issue for bugs. For security problems follow the reporting steps in [SECURITY.md](SECURITY.md).
