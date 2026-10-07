# Sortify Auto-Merge Policy and Architecture

## Overview

Sortify uses a safe, risk-aware auto-merge system designed to reduce routine PR coordination and branch-conflict churn on `main` without compromising security or code quality.

Because `fderuiter/sortify` is owned by a personal GitHub account (where native GitHub Merge Queue is unavailable), the system uses a **serialized auto-merge controller** backed by GitHub Actions `concurrency`, mandatory `main` branch freshness re-validation, and durable required status gates.

---

## 1. Risk Classification & Eligibility

Every Pull Request undergoes automated risk classification via `.github/workflows/classify-pr.yml` and `scripts/classify_pr.py`.

### A. Automatically Eligible by Default
- **Documentation-only changes** (`docs/**`, `README.md`, `mkdocs.yml`) that do not touch sensitive/security files.
- **Dependabot minor or patch updates** (pip or github-actions) after full CI verification.

### B. Explicit Opt-In (`automerge` label) vs Classifier Approval (`automerge:eligible`)
Production-code maintenance and refactor PRs require an explicit **`automerge`** label to authorize automated evaluation.
When present, `.github/workflows/classify-pr.yml` runs `scripts/classify_pr.py` to evaluate the PR. If low-risk criteria are met, the classifier applies the **`automerge:eligible`** label and posts a successful `automerge/classification` commit status.
The auto-merge controller strictly requires `automerge:eligible` (not raw `automerge`) to proceed.

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
- Release & compilation toolchains (`scripts/build.py`, `smart-autosorter.spec`)
- Major dependency updates (e.g. major version bumps)
- Snapshot baselines (`tests/snapshots/api/**`, `tests/snapshots/tui_svg/**`)

---

## 2. Conflict Handling & Serialization

1. **Serialized Execution:** Automated PR evaluation and final merge preparation are strictly serialized using GitHub Actions `concurrency: group: auto-merge-controller`.
2. **Current-Main Validation:** Before merging, the controller verifies if the candidate branch is behind `main`. If behind, it requests a branch update via the GitHub API.
3. **No Automatic Conflict Resolution:** If updating a branch results in Git merge conflicts, the controller:
   - Withholds auto-merge eligibility.
   - Applies the `automerge:conflict` and `automerge:blocked` labels.
   - Leaves source code intact for developer resolution.

---

## 3. Emergency Overrides & Disabling Automation

- **Disable Auto-Merge for a Specific PR:** Apply the `disable-automerge` label to the PR.
- **Global Emergency Override:** Disable `.github/workflows/auto-merge-controller.yml` in GitHub Actions settings or remove the workflow file. `main` branch protection rules remain active regardless of controller status.

---

## 4. Organization Migration Path (Future Work)

If `fderuiter/sortify` is transferred to a GitHub Organization in the future:
1. Enable native **GitHub Merge Queue** on the `main` branch ruleset.
2. Add `merge_group:` event triggers to `.github/workflows/ci.yml`.
3. Deprecate `.github/workflows/auto-merge-controller.yml`.
