"""Regression test for #716: rollback must not start while other moves are in flight."""

import os
import time
from unittest.mock import patch

import pytest

import app.core.resilient_file_ops as rfo
from app.core.mover import execute_moves


def test_failed_move_rolls_back_every_file(test_history_env):
    base_dir, db, _cache, history_manager, _worker = test_history_env

    names = [f"f{i:02d}.txt" for i in range(40)]
    for name in names:
        with open(os.path.join(base_dir, name), "w") as fh:
            fh.write(name)

    plan = {
        "Cat": {
            name: {
                "__type__": "file",
                "relative_source": f"../{name}",
                "target_filename": name,
            }
            for name in names
        }
    }

    real_move = rfo.atomic_move_non_clobber

    def slow_or_locked(src, dst):
        if os.path.basename(src) == "f00.txt":
            raise PermissionError("file locked by another process")
        time.sleep(0.2)
        return real_move(src, dst)

    with (
        patch.object(rfo, "atomic_move_non_clobber", side_effect=slow_or_locked),
        patch.object(rfo, "RETRY_DELAY", 0.001),
    ):
        with pytest.raises(PermissionError, match="file locked"):
            execute_moves(
                base_dir, plan, db, history_manager, max_workers=4, chunk_size=40
            )

    for name in names:
        path = os.path.join(base_dir, name)
        assert os.path.exists(path), f"{name} was lost"
        with open(path) as fh:
            assert fh.read() == name
    cat_dir = os.path.join(base_dir, "Cat")
    assert not os.path.exists(cat_dir) or not os.listdir(cat_dir)
