"""Regression test for #866: undo must move aside a file sitting where a folder belongs."""

import os
import shutil

from app.core.mover import execute_moves


class Settings:
    CONFLICT_POLICY = "rename"


PLAN = {
    "Finance": {
        "report.txt": {
            "__type__": "file",
            "relative_source": "../a/report.txt",
            "target_filename": "report.txt",
        }
    }
}


def _read(path):
    with open(path) as fh:
        return fh.read()


def test_undo_moves_aside_a_file_blocking_a_restored_folder(test_history_env):
    base_dir, db, _cache, history_manager, _worker = test_history_env
    os.makedirs(os.path.join(base_dir, "a"))
    os.makedirs(os.path.join(base_dir, "Finance"))
    with open(os.path.join(base_dir, "a", "report.txt"), "w") as fh:
        fh.write("report")

    execute_moves(base_dir, PLAN, db, history_manager, Settings())
    session_id = history_manager.get_sessions()[0]["session_id"]
    shutil.rmtree(os.path.join(base_dir, "a"), ignore_errors=True)
    blocker = os.path.join(base_dir, "a")
    with open(blocker, "w") as fh:
        fh.write("user file")

    history_manager.rollback(session_id, ignore_missing=True)

    assert _read(os.path.join(base_dir, "a", "report.txt")) == "report"
    assert _read(os.path.join(base_dir, "a_1")) == "user file"
    assert not os.path.exists(os.path.join(base_dir, "Finance", "report.txt"))
