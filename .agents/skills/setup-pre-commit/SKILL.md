---
name: setup-pre-commit
description: "Add or change a commit-time check in this repo's pre-commit framework config (`.pre-commit-config.yaml`). Use when the user wants a new hook, a hook scoped to staged files, or commit-time formatting, linting, type checking or validation."
license: MIT
metadata:
  author: Matt Pocock (adapted for Sortify)
  version: "1.1"
---

# Setup Pre-Commit Hooks

Sortify uses the Python [pre-commit](https://pre-commit.com) framework, driven through `uv`. The config already exists; this skill adds or changes hooks in it. It never introduces a second hook system (Husky, lint-staged, a hand-written `.git/hooks/pre-commit`).

## Steps

### 1. Read what is there

Read `.pre-commit-config.yaml`. Note the two kinds of hook it holds:

- **Upstream hooks** under a `repo: <git URL>` entry with a pinned `rev`.
- **Local hooks** under `repo: local`, run as `language: system` with an `entry` that starts with `uv run`, so they use the project's locked tool versions.

If the check the user wants already exists, stop and point at it.

### 2. Pick the hook shape

- A tool already in the `dev` dependency group (ruff, ty, pytest) → a **local** hook with `entry: uv run <tool> ...`. Adding a new tool means adding it to the `dev` group in `pyproject.toml` first, with `uv add --group dev <tool>`.
- A generic file check that `pre-commit/pre-commit-hooks` already ships → add its `id` to that existing upstream entry.
- A repo validator under `scripts/` → a local hook with `entry: uv run python scripts/<name>.py`.

### 3. Scope it to the files it checks

- Set `files:` (a regex) or `types: [python]` so the hook runs only when relevant files are staged.
- Leave `pass_filenames` at its default (true) when the tool accepts file arguments, so it checks only staged files. Set `pass_filenames: false` only for whole-repo checks, and give those a tight `files:` regex so they don't run on every commit.
- Keep commit-time hooks fast. Anything over a few seconds (the full test suite, builds) belongs in CI, not here.

### 4. Write the hook

Match the style of the neighbouring local hooks:

```yaml
      - id: deptry
        name: Deptry Dependency Check
        entry: uv run deptry .
        language: system
        pass_filenames: false
        files: '^(app/.*\.py|pyproject\.toml|uv\.lock)$'
```

### 5. Verify

- [ ] `uv run pre-commit validate-config` passes
- [ ] `uv run pre-commit run <id> --all-files` runs the new hook and its result is what you expect
- [ ] Staging one relevant file and running `uv run pre-commit run <id>` triggers it; staging an unrelated file skips it

### 6. Commit

Commit `.pre-commit-config.yaml` (and `pyproject.toml` / `uv.lock` if a tool was added) on a feature branch. The `no-commit-to-branch` hook blocks commits to `main`.

## Notes

- Contributors install the hooks with `uv run pre-commit install`.
- `uv run pre-commit autoupdate` bumps the `rev` of upstream hooks; review the diff before committing it.
