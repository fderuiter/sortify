---
status: accepted (2026-10-19)
---

# Auto-merge decides by path tier, and data-safety code always needs a human

Bots write most of Sortify's changes, and `scripts/auto_merge_controller.py` merges the ones `scripts/classify_pr.py` calls low-risk. Every changed path falls into one of three tiers, and the PR takes the strictest tier any of its files hits:

1. **Docs** (`docs/`, `README.md`, `mkdocs.yml`): auto-merge once `Required CI` passes on the current head.
2. **Code and tests**: auto-merge only with the `automerge` label applied by a maintainer (a person with write access, not a bot), within the size limits, with `Required CI` green. Tests stay here rather than with docs, because a bot that deletes or weakens a failing test would otherwise merge unreviewed.
3. **Protected paths**: never auto-merged; the owner must approve. These are the security paths already listed (`.github/`, `SECURITY.md`, `PRIVACY.md`, `app/config.py`, `app/core/crypto.py`, `scripts/`, the PyInstaller spec, `network_rules.json`, `.gitleaks.toml`, dependency manifests outside Dependabot) plus every module that can lose a user's file: `app/core/mover.py`, `resilient_file_ops.py`, `ledger.py`, `history.py`, `session.py`, `db.py`, `db_conn.py`, `db_worker.py` and `quarantine_interceptor.py`, and snapshot baselines.

`PROTECTED_PATHS` in `scripts/classify_pr.py` is the one list of protected paths, written in CODEOWNERS syntax. `.github/CODEOWNERS` has to list every entry, and `tests/test_risk_classifier.py` fails when the two disagree, so GitHub's code-owner review and the controller enforce the same boundary. A Dependabot bump counts as major from the version numbers in its title (a new first component, or a new second one under 0.x); titles without one `from A to B`, such as grouped updates, count as major. Only the exact `dependabot[bot]` login counts as Dependabot; a `dependencies` label does not.

We chose this because the gate today leaves the code that matters most open. The classifier protects `mover.py`, `history.py` and `session.py`, but not `resilient_file_ops.py`, `ledger.py` or the `db*` modules, which is where the move and undo bugs fixed in #757 to #768 lived. `CODEOWNERS` and `SENSITIVE_PATHS` are separate lists that already differ (`CODEOWNERS` lacks `scripts/` and the spec file). The `automerge` opt-in is a label the PR's own author can add. And "major" dependency bumps are detected by searching the PR title for "major" or "breaking", which Dependabot titles don't contain.

## Considered options

- **Turn auto-merge off.** Safest, but the owner merges in bulk today and the bots open about 15 PRs a day; without a gate, the review load lands on one person and gets skipped.
- **Require human review on everything except docs.** Simple, but it sends routine UI and test changes to the same queue as mover changes, so the queue stops being read.
- **Gate on test coverage of the changed lines instead of paths.** Better in principle, but the coverage gate is a no-op today (#738), and coverage doesn't show whether a test would catch data loss.

## Consequences

- PRs touching the protected data-safety modules wait for the owner even when CI is green. That is the intent until the property test from the system design (random plans against a folder of unrelated and hidden files, killed at random points, then reconciled and undone, with every original file byte-identical) runs in `Required CI`. After that, revisit whether `db*` can drop to tier 2.
- The label check costs two API calls per labelled PR (issue events, then the labeller's permission) and fails closed: if either call fails, the label is ignored.
- Adding a protected path means editing `PROTECTED_PATHS` and `CODEOWNERS` together, which the test enforces.
- The `classify-pr.yml` workflow still guesses major bumps from the title when it labels a PR. Its labels only pre-filter: the controller re-classifies with this logic before merging, so the gate holds, but the labels can say `automerge:eligible` for a PR the controller will refuse.
- Branch protection must require code-owner review for this to hold when someone merges by hand. That setting can't be seen from the repository, so it has to be confirmed in GitHub's settings.
