"""Regression tests for #766: the overwrite policy must not delete the file it replaces."""

import os
from unittest.mock import patch

import pytest

import app.core.resilient_file_ops as rfo
from app.core.history import _purge_overwrite_backups
from app.core.mover import OVERWRITE_BACKUP_DIR, execute_moves, overwrite_backup_dir


class OverwriteSettings:
    CONFLICT_POLICY = "overwrite"


PLAN = {
    "Docs": {
        "report.txt": {
            "__type__": "file",
            "relative_source": "../report.txt",
            "target_filename": "report.txt",
        }
    }
}


def _setup(base_dir):
    os.makedirs(os.path.join(base_dir, "Docs"))
    src = os.path.join(base_dir, "report.txt")
    dst = os.path.join(base_dir, "Docs", "report.txt")
    with open(src, "w") as fh:
        fh.write("new report")
    with open(dst, "w") as fh:
        fh.write("old report")
    return src, dst


def _read(path):
    with open(path) as fh:
        return fh.read()


def _latest_session(history_manager):
    return history_manager.get_sessions()[0]["session_id"]


def test_overwrite_keeps_replaced_file_and_undo_restores_it(test_history_env):
    base_dir, db, _cache, history_manager, _worker = test_history_env
    src, dst = _setup(base_dir)

    execute_moves(base_dir, PLAN, db, history_manager, OverwriteSettings())

    assert _read(dst) == "new report"
    assert not os.path.exists(src)
    session_id = _latest_session(history_manager)
    backup = os.path.join(
        overwrite_backup_dir(base_dir, session_id), "Docs", "report.txt"
    )
    assert _read(backup) == "old report"

    history_manager.unwind_session(session_id, db=db)

    assert _read(src) == "new report"
    assert _read(dst) == "old report"
    assert not os.path.exists(os.path.join(base_dir, OVERWRITE_BACKUP_DIR))


def test_failed_overwrite_puts_replaced_file_back(test_history_env):
    base_dir, db, _cache, history_manager, _worker = test_history_env
    src, dst = _setup(base_dir)

    real_move = rfo.atomic_move_non_clobber

    def locked_source(move_src, move_dst):
        if os.path.normcase(move_src) == os.path.normcase(src):
            raise PermissionError("file locked by another process")
        return real_move(move_src, move_dst)

    with (
        patch.object(rfo, "atomic_move_non_clobber", side_effect=locked_source),
        patch.object(rfo, "RETRY_DELAY", 0.001),
    ):
        with pytest.raises(PermissionError, match="file locked"):
            execute_moves(base_dir, PLAN, db, history_manager, OverwriteSettings())

    assert _read(src) == "new report"
    assert _read(dst) == "old report"


def test_pruning_a_session_purges_its_backups(tmp_path):
    base_dir = str(tmp_path)
    keep = overwrite_backup_dir(base_dir, "keep")
    drop = overwrite_backup_dir(base_dir, "drop")
    for folder in (keep, drop):
        os.makedirs(os.path.join(folder, "Docs"))
        with open(os.path.join(folder, "Docs", "report.txt"), "w") as fh:
            fh.write("old")

    _purge_overwrite_backups(base_dir, "drop")

    assert not os.path.exists(drop)
    assert os.path.exists(os.path.join(keep, "Docs", "report.txt"))

    _purge_overwrite_backups(base_dir, "keep")
    assert not os.path.exists(os.path.join(base_dir, OVERWRITE_BACKUP_DIR))


def test_undo_matches_paths_written_with_other_separators(
    test_history_env, monkeypatch
):
    """Undo must treat two spellings of one path as the same file.

    On Windows the scanner returns backslash paths while the snapshot stores
    forward slashes, so the restore loop saw ``Docs\\report.txt`` and
    ``Docs/report.txt`` as different files and moved the wrong one back.
    A ``./`` segment gives the same mismatch on any platform.
    """
    import app.core.scanner as scanner

    base_dir, db, _cache, history_manager, _worker = test_history_env
    src, dst = _setup(base_dir)
    execute_moves(base_dir, PLAN, db, history_manager, OverwriteSettings())
    session_id = _latest_session(history_manager)

    real_scan = scanner.get_files_recursively

    def scan_with_other_spelling(*args, **kwargs):
        return [
            os.path.join(os.path.dirname(p), ".", os.path.basename(p))
            for p in real_scan(*args, **kwargs)
        ]

    monkeypatch.setattr(scanner, "get_files_recursively", scan_with_other_spelling)
    history_manager.unwind_session(session_id, db=db)

    assert _read(src) == "new report"
    assert _read(dst) == "old report"
