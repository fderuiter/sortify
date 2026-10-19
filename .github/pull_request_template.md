## Description

Please provide a summary of the changes in this pull request.

## Auto-Merge Eligibility

- [ ] Low-risk change (routine bugfix, docs, or maintenance)
- [ ] No sensitive paths modified (`.github/`, `SECURITY.md`, `app/config.py`, `app/core/crypto.py`, `app/core/mover.py`, etc.)
- [ ] No snapshots or API contract baselines modified
- [ ] Tests added/updated as appropriate
- [ ] Safe for automatic merge once required CI succeeds (add `automerge` label if production code)

## Documentation Synchronization Check

- [ ] I have updated the docstrings for any new or modified public APIs.
- [ ] I have run `uv run python scripts/generate_docs.py --check` and committed any regenerated files.
- [ ] I have included the necessary API reference updates if applicable.

## Database & Configuration Check

- [ ] I have verified that database schema migrations execute sequentially and have been tested.
- [ ] I have verified that configuration schema mutations, bounds, and validations have been tested.

## Terminal UI Check

- [ ] For TUI changes, I have run the app (`uv run smart-autosorter --tui`) and checked the affected screens at a narrow terminal width (80 columns).
- [ ] Keyboard navigation and screen-reader announcements still work for new or changed widgets.
- [ ] I have updated TUI snapshots (`UPDATE_SNAPSHOTS=1 uv run pytest tests/test_visual_snapshots.py -n 0`) only for intended visual changes.
