# Main Branch Protection & Repository Ruleset Configuration

This document specifies the required branch protection ruleset for `main` in **Sortify**.

## Ruleset Definition

The canonical ruleset export is stored at `.github/rulesets/main.json`.

### Settings Overview
- **Target Branch:** `refs/heads/main`
- **Restrict Deletions:** Enabled (prevents deleting `main`).
- **Block Force Pushes:** Enabled (prevents non-fast-forward updates).
- **Require Pull Request Before Merging:**
  - Code Owner Reviews: Required (`require_code_owner_review: true` with `required_approving_review_count: 0` and no `bypass_actors`). `.github/CODEOWNERS` lists only Tier 3 protected paths (`PROTECTED_PATHS` in `scripts/classify_pr.py`) and snapshot/type-check baselines (`tests/snapshots/api/`, `tests/snapshots/tui_svg/`, `tests/ty_baseline.json`) with no `*` catch-all rule, so Tier 1 and maintainer-labelled Tier 2 PRs can auto-merge while Tier 3 changes require `@fderuiter` approval.
  - Require Conversation Resolution: All review threads must be resolved before merge.
- **Required Status Checks:**
  - Require branches to be up to date before merging (`strict_required_status_checks_policy: true`).
  - Required Check Name: `Required CI` (durable aggregate GitHub Actions check run from `.github/workflows/ci.yml`).
  - `workflow_dispatch` Orchestration & Fork Limits: Because `GITHUB_TOKEN` updates and merges do not trigger `pull_request` or `push` workflows, `scripts/auto_merge_controller.py` dispatches `.github/workflows/ci.yml` (`workflow_dispatch`) on updated in-repository PR branches to post `Required CI` on the new head SHA, and dispatches `.github/workflows/ci.yml` on `main` after merging so `main` verification and `doc-deploy` run. External fork branches that fall behind `main` cannot be dispatched via `workflow_dispatch` and are skipped until the contributor updates or rebases the fork branch.
- **Linear History:** Required (Squash merge strategy).

## Manual Configuration Instructions (GitHub UI)

If rulesets are configured via the GitHub web UI:

1. Navigate to **Settings** > **Rules** > **Rulesets**.
2. Click **New ruleset** > **Import a ruleset**.
3. Select `.github/rulesets/main.json` from the repository.
4. Set Enforcement Status to **Active**.
5. Save changes.
