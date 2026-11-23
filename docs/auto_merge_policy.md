# Sortify Auto-Merge Policy and Architecture

## Overview

Sortify uses a safe, risk-aware auto-merge system designed to reduce routine PR coordination and branch-conflict churn on `main` without compromising security or code quality.

Because `fderuiter/sortify` is owned by a personal GitHub account (where native GitHub Merge Queue is unavailable), the system uses a **serialized auto-merge controller** backed by GitHub Actions `concurrency`, mandatory `main` branch freshness re-validation, and durable required status gates.

---

## 1. Risk Classification & Eligibility

Every Pull Request undergoes automated risk classification via `.github/workflows/classify-pr.yml` and `scripts/classify_pr.py`.

### A. Automatically Eligible by Default
- **Documentation-only changes** (`docs/**`, `README.md`, `mkdocs.yml`) that do not touch sensitive/security files.
- **Dependabot minor or patch updates** to `pyproject.toml`/`uv.lock` after full CI verification. Only PRs authored by `dependabot[bot]` or `dependabot` qualify; a `dependencies` label alone does not. GitHub Actions bumps touch `.github/**` and always need a human merge.

### B. Explicit Opt-In (`automerge` label) vs Classifier Approval (`automerge:eligible`)
Production-code maintenance and refactor PRs require an explicit **`automerge`** label to authorize automated evaluation.
Autonomous agent PRs (including `BOT_AUTHORS`: `stitch`, `jules`, `github-actions[bot]`, `dependabot[bot]`, `dependabot`) and automated security patches do not bypass review: they require an explicit maintainer-applied **`automerge`** label (applied by a human maintainer with write access or above) before automated evaluation or merging can proceed.
When present, `.github/workflows/classify-pr.yml` runs `scripts/classify_pr.py` to evaluate the PR. If low-risk criteria are met, the classifier applies the **`automerge:eligible`** label and posts a successful `automerge/classification` commit status.
The auto-merge controller strictly requires `automerge:eligible` (not raw `automerge`) to proceed, and verifies via event history that the `automerge` label was added by a human maintainer rather than an automated bot or untrusted author.

### Trust model
The PR under evaluation must not be able to approve itself:
- `classify-pr.yml` runs on `pull_request_target`, so both the workflow and `scripts/classify_pr.py` come from the base branch. It never checks out or runs the PR's code.
- The `automerge:eligible` label and the `automerge/classification` status are only a pre-filter. Before acting, the controller re-runs `classify_pr()` from its own default-branch checkout on file, commit and label data fetched from the GitHub API.
- `Required CI` is accepted only as a check run created by GitHub Actions. Plain commit statuses with that name are ignored.

Thresholds for low-risk opt-in PRs:
- $\le 10$ changed files
- $\le 500$ non-generated changed lines
- No sensitive paths touched
- No snapshot or API baseline modifications
- Not draft

*(Note: Resolved review conversations are mandatory and enforced directly by GitHub main branch rulesets.)*

### C. Never Auto-Merge (Human Review Mandatory)
The controller **fails closed** and blocks auto-merge for PRs touching any of the following sensitive categories:
- Workflow or action configuration (`.github/**`)
- Security & privacy policies (`SECURITY.md`, `PRIVACY.md`, `.gitleaks.toml`)
- Application configuration & schema (`app/config.py`, `network_rules.json`)
- Envelope encryption & key management (`app/core/crypto.py`)
- Safe file movement & rollback logic (`app/core/mover.py`, `app/core/history.py`)
- Quarantine & interceptor logic (`app/core/quarantine_interceptor.py`, `app/core/session.py`)
- Release & compilation toolchains and CI/merge tooling (`scripts/**`, `smart-autosorter.spec`)
- Dependency manifests and lockfile (`pyproject.toml`, `uv.lock`, `offline_bundle/requirements.txt`), unless the PR is an approved native Dependabot update (`dependabot[bot]`) or an automated security patch with a maintainer-applied `automerge` label
- Major dependency updates (e.g. major version bumps)
- Snapshot baselines (`tests/snapshots/api/**`, `tests/snapshots/tui_svg/**`)

---

## 2. Conflict Handling & Serialization

1. **Serialized Execution:** Automated PR evaluation and final merge preparation are serialized at the `jobs.auto-merge` level using GitHub Actions `concurrency: group: auto-merge-controller` with `cancel-in-progress: false`. Placing `concurrency` after the job `if` filter ensures unrelated failed or cancelled `CI Pipeline` runs skip the job before acquiring the lock and never abort an active merge.
2. **Current-Main Validation & `workflow_dispatch` Orchestration:** Because branch updates and squash merges performed with `GITHUB_TOKEN` suppress downstream `pull_request` and `push` events, `scripts/auto_merge_controller.py` explicitly orchestrates `.github/workflows/ci.yml` via `workflow_dispatch`:
   - **In-repository stale branches:** When `mergeable_state == "behind"` and the PR's head branch belongs to `fderuiter/sortify`, the controller updates the branch via the GitHub `update-branch` API, polls pull request metadata briefly until the new head SHA is visible, and dispatches `.github/workflows/ci.yml` on that branch so `Required CI` runs on the updated commit.
   - **Post-merge `main` verification:** After `merge_pull_request` completes a squash merge, the controller dispatches `.github/workflows/ci.yml` on `main` so post-merge verification and `doc-deploy` run (logging any transient dispatch error without failing the completed merge).
   - **Fork-branch limits:** `GITHUB_TOKEN` cannot update or dispatch `.github/workflows/ci.yml` against branches in external forks. Stale fork PRs (`mergeable_state == "behind"`) are skipped without blocking other eligible PRs in the queue until a contributor updates or rebases the fork branch onto `main` (which triggers normal `pull_request` CI). Up-to-date fork PRs with a passing `Required CI` check run still merge automatically.
3. **No Automatic Conflict Resolution:** If updating a branch results in Git merge conflicts, the controller:
   - Withholds auto-merge eligibility.
   - Applies the `automerge:conflict` and `automerge:blocked` labels.
   - Leaves source code intact for developer resolution.

---

## 3. Emergency Overrides & Disabling Automation

- **Disable Auto-Merge for a Specific PR:** Apply the `disable-automerge` label to the PR.
- **Global Emergency Override:** Disable `.github/workflows/auto-merge-controller.yml` in GitHub Actions settings or remove the workflow file. `main` branch protection rules remain active regardless of controller status.
- **Clear a Backed-Up Actions Queue:** Run **Cancel All Runs** (`.github/workflows/cancel-queued-runs.yml`) from the Actions tab. With the defaults one run cancels every queued and running workflow run on every branch. Inputs narrow it to queued or running runs, one branch or one workflow, force-cancel stuck runs, re-run the newest cancelled run of each workflow once per branch (`requeue`), or only list what would happen (`dry_run`).

---

## 4. Organization Migration Path (Future Work)

If `fderuiter/sortify` is transferred to a GitHub Organization in the future:
1. Enable native **GitHub Merge Queue** on the `main` branch ruleset.
2. Add `merge_group:` event triggers to `.github/workflows/ci.yml`.
3. Deprecate `.github/workflows/auto-merge-controller.yml`.
