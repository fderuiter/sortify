# Main Branch Protection & Repository Ruleset Configuration

This document specifies the required branch protection ruleset for `main` in **Sortify**.

## Ruleset Definition

The canonical ruleset export is stored at `.github/rulesets/main.json`.

### Settings Overview
- **Target Branch:** `refs/heads/main`
- **Restrict Deletions:** Enabled (prevents deleting `main`).
- **Block Force Pushes:** Enabled (prevents non-fast-forward updates).
- **Require Pull Request Before Merging:**
  - Code Owner Reviews: Required for sensitive paths.
  - Require Conversation Resolution: All review threads must be resolved before merge.
- **Required Status Checks:**
  - Require branches to be up to date before merging (`strict_required_status_checks_policy: true`).
  - Required Check Name: `Required CI` (durable aggregate status from `.github/workflows/ci.yml`).
- **Linear History:** Required (Squash merge strategy).

## Manual Configuration Instructions (GitHub UI)

If rulesets are configured via the GitHub web UI:

1. Navigate to **Settings** > **Rules** > **Rulesets**.
2. Click **New ruleset** > **Import a ruleset**.
3. Select `.github/rulesets/main.json` from the repository.
4. Set Enforcement Status to **Active**.
5. Save changes.
