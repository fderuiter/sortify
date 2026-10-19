"""Regression tests for #719: cleanup only prunes folders the sort emptied."""

import os

from app.core.mover import execute_moves


def test_sort_keeps_preexisting_empty_and_hidden_dirs(test_history_env):
    base_dir, db, _cache, history_manager, _worker = test_history_env

    for rel in [".git/refs/heads", ".git/refs/tags", "Inbox_for_scans", "old/sub"]:
        os.makedirs(os.path.join(base_dir, rel))
    with open(os.path.join(base_dir, "old", "sub", "notes.txt"), "w") as fh:
        fh.write("notes")

    plan = {
        "Cat": {
            "notes.txt": {
                "__type__": "file",
                "relative_source": "../old/sub/notes.txt",
                "target_filename": "notes.txt",
            }
        }
    }
    execute_moves(base_dir, plan, db, history_manager)

    assert os.path.exists(os.path.join(base_dir, "Cat", "notes.txt"))
    assert os.path.isdir(os.path.join(base_dir, ".git", "refs", "heads"))
    assert os.path.isdir(os.path.join(base_dir, ".git", "refs", "tags"))
    assert os.path.isdir(os.path.join(base_dir, "Inbox_for_scans"))
    # The folders this sort emptied are pruned up to base_dir.
    assert not os.path.exists(os.path.join(base_dir, "old"))
