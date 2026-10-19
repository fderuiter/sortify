"""Regression test for #865: `undo --force` must skip a sorted file the user deleted."""

import os

from app.core.mover import execute_moves


class Settings:
    CONFLICT_POLICY = "rename"


PLAN = {
    "Docs": {
        name: {
            "__type__": "file",
            "relative_source": f"../{name}",
            "target_filename": name,
        }
        for name in ("keep.txt", "gone.txt")
    }
}


def test_force_undo_skips_deleted_file_and_restores_the_rest(test_history_env):
    base_dir, db, _cache, history_manager, _worker = test_history_env
    os.makedirs(os.path.join(base_dir, "Docs"))
    for name in ("keep.txt", "gone.txt"):
        with open(os.path.join(base_dir, name), "w") as fh:
            fh.write(name)

    execute_moves(base_dir, PLAN, db, history_manager, Settings())
    session_id = history_manager.get_sessions()[0]["session_id"]
    os.remove(os.path.join(base_dir, "Docs", "gone.txt"))

    history_manager.rollback(session_id, ignore_missing=True)

    assert os.path.exists(os.path.join(base_dir, "keep.txt"))
    assert not os.path.exists(os.path.join(base_dir, "Docs", "keep.txt"))
    assert not os.path.exists(os.path.join(base_dir, "gone.txt"))
