"""Regression test for #767: a sort must not move files it could not undo."""

import os
from unittest.mock import patch

import pytest

from app.core.mover import execute_moves


def _plan(names):
    return {
        "Cat": {
            name: {
                "__type__": "file",
                "relative_source": f"../{name}",
                "target_filename": name,
            }
            for name in names
        }
    }


@pytest.mark.parametrize(
    "snapshot_patch",
    [
        {"side_effect": OSError("disk full")},
        {"return_value": None},
    ],
    ids=["raises", "returns-none"],
)
def test_sort_refuses_without_snapshot(test_history_env, snapshot_patch):
    base_dir, db, _cache, history_manager, _worker = test_history_env

    names = ["a.txt", "b.txt"]
    for name in names:
        with open(os.path.join(base_dir, name), "w") as fh:
            fh.write(name)

    with patch.object(history_manager, "create_snapshot", **snapshot_patch):
        with pytest.raises(RuntimeError, match="undo snapshot"):
            execute_moves(base_dir, _plan(names), db, history_manager)

    for name in names:
        assert os.path.exists(os.path.join(base_dir, name)), f"{name} was moved"
    assert not os.path.exists(os.path.join(base_dir, "Cat", "a.txt"))
