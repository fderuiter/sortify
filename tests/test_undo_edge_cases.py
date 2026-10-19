"""Undo must put every sorted file back without destroying anything the user made since.

Each test runs the real ``execute_moves`` on a real folder under ``tmp_path``,
changes the folder the way a user might, then calls ``HistoryManager.rollback``,
which is what ``smart-autosorter undo`` calls.
"""

import hashlib
import os

import pytest

from app.core.mover import execute_moves


class KeepBothSettings:
    CONFLICT_POLICY = "rename"


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def _read(path):
    with open(path) as fh:
        return fh.read()


def _entry(rel_source, target_filename):
    return {
        "__type__": "file",
        "relative_source": rel_source,
        "target_filename": target_filename,
    }


def _sort(base_dir, db, history_manager, plan):
    execute_moves(base_dir, plan, db, history_manager, KeepBothSettings())
    return history_manager.get_sessions()[0]["session_id"]


def _manifest(base_dir):
    """Map every file's relative path to a hash of its bytes."""
    out = {}
    for root, _dirs, files in os.walk(base_dir):
        for name in files:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, base_dir).replace(os.sep, "/")
            with open(path, "rb") as fh:
                out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


def _user_files(manifest):
    """Drop the app's own bookkeeping files so tests compare only user files."""
    return {k: v for k, v in manifest.items() if not k.startswith(".")}


def test_undo_restores_nested_sources_exactly(test_history_env):
    base_dir, db, _cache, history_manager, _worker = test_history_env
    _write(os.path.join(base_dir, "inbox", "a", "report.txt"), "report")
    _write(os.path.join(base_dir, "inbox", "b", "invoice.txt"), "invoice")
    before = _user_files(_manifest(base_dir))

    session_id = _sort(
        base_dir,
        db,
        history_manager,
        {
            "Finance": {
                "report.txt": _entry("../inbox/a/report.txt", "report.txt"),
                "invoice.txt": _entry("../inbox/b/invoice.txt", "invoice.txt"),
            }
        },
    )
    assert _read(os.path.join(base_dir, "Finance", "report.txt")) == "report"

    history_manager.rollback(session_id)

    assert _user_files(_manifest(base_dir)) == before


def _sort_then_block_folder(base_dir, db, history_manager):
    """Sort ``a/report.txt`` away, then save a user file named ``a``."""
    _write(os.path.join(base_dir, "a", "report.txt"), "report")
    session_id = _sort(
        base_dir,
        db,
        history_manager,
        {"Finance": {"report.txt": _entry("../a/report.txt", "report.txt")}},
    )
    if os.path.isdir(os.path.join(base_dir, "a")):
        os.rmdir(os.path.join(base_dir, "a"))
    _write(os.path.join(base_dir, "a"), "user's new file")
    return session_id


def _contents(base_dir):
    return sorted(
        _read(os.path.join(root, name))
        for root, _dirs, files in os.walk(base_dir)
        for name in files
        if not name.startswith(".")
    )


def test_undo_never_destroys_a_file_that_blocks_a_folder_it_must_restore(
    test_history_env,
):
    base_dir, db, _cache, history_manager, _worker = test_history_env
    session_id = _sort_then_block_folder(base_dir, db, history_manager)

    try:
        history_manager.rollback(session_id, ignore_missing=True)
    except (PermissionError, ValueError):
        pass

    assert _contents(base_dir) == ["report", "user's new file"]


def test_undo_moves_a_blocking_file_aside_and_restores_the_folder(
    test_history_env,
):
    base_dir, db, _cache, history_manager, _worker = test_history_env
    session_id = _sort_then_block_folder(base_dir, db, history_manager)

    history_manager.rollback(session_id, ignore_missing=True)

    assert _read(os.path.join(base_dir, "a", "report.txt")) == "report"
    assert _contents(base_dir) == ["report", "user's new file"]


def test_undo_never_loses_edits_made_to_a_sorted_file(test_history_env):
    """Undo may refuse a file that changed since the sort, but must keep the edits."""
    base_dir, db, _cache, history_manager, _worker = test_history_env
    _write(os.path.join(base_dir, "notes.txt"), "draft")

    session_id = _sort(
        base_dir,
        db,
        history_manager,
        {"Docs": {"notes.txt": _entry("../notes.txt", "notes.txt")}},
    )
    _write(os.path.join(base_dir, "Docs", "notes.txt"), "draft plus edits")

    with pytest.raises(ValueError, match="hash mismatch"):
        history_manager.rollback(session_id, ignore_missing=True)

    assert _read(os.path.join(base_dir, "Docs", "notes.txt")) == "draft plus edits"
    assert _contents(base_dir) == ["draft plus edits"]


def test_undo_restores_two_identical_files_to_their_own_folders(test_history_env):
    """Same name, same bytes: the sort renames one, undo must restore both."""
    base_dir, db, _cache, history_manager, _worker = test_history_env
    _write(os.path.join(base_dir, "a", "scan.txt"), "same bytes")
    _write(os.path.join(base_dir, "b", "scan.txt"), "same bytes")
    before = _user_files(_manifest(base_dir))

    session_id = _sort(
        base_dir,
        db,
        history_manager,
        {
            "Scans": {
                "scan.txt": _entry("../a/scan.txt", "scan.txt"),
                "scan_b.txt": _entry("../b/scan.txt", "scan.txt"),
            }
        },
    )
    sorted_files = os.listdir(os.path.join(base_dir, "Scans"))
    assert len(sorted_files) == 2, sorted_files

    history_manager.rollback(session_id)

    assert _user_files(_manifest(base_dir)) == before


def _sort_two_then_delete_one(base_dir, db, history_manager):
    _write(os.path.join(base_dir, "keep.txt"), "keep")
    _write(os.path.join(base_dir, "gone.txt"), "gone")
    session_id = _sort(
        base_dir,
        db,
        history_manager,
        {
            "Docs": {
                "keep.txt": _entry("../keep.txt", "keep.txt"),
                "gone.txt": _entry("../gone.txt", "gone.txt"),
            }
        },
    )
    os.remove(os.path.join(base_dir, "Docs", "gone.txt"))
    return session_id


def test_forced_undo_with_a_deleted_sorted_file_keeps_the_rest(test_history_env):
    base_dir, db, _cache, history_manager, _worker = test_history_env
    session_id = _sort_two_then_delete_one(base_dir, db, history_manager)

    try:
        history_manager.rollback(session_id, ignore_missing=True)
    except ValueError:
        pass

    assert _contents(base_dir) == ["keep"]


def test_forced_undo_with_a_deleted_sorted_file_succeeds(test_history_env):
    base_dir, db, _cache, history_manager, _worker = test_history_env
    session_id = _sort_two_then_delete_one(base_dir, db, history_manager)

    history_manager.rollback(session_id, ignore_missing=True)

    assert _read(os.path.join(base_dir, "keep.txt")) == "keep"
    assert not os.path.exists(os.path.join(base_dir, "gone.txt"))


def test_undo_twice_does_not_move_files_again(test_history_env):
    base_dir, db, _cache, history_manager, _worker = test_history_env
    _write(os.path.join(base_dir, "report.txt"), "report")
    before = _user_files(_manifest(base_dir))

    session_id = _sort(
        base_dir,
        db,
        history_manager,
        {"Docs": {"report.txt": _entry("../report.txt", "report.txt")}},
    )
    history_manager.rollback(session_id)
    try:
        history_manager.rollback(session_id)
    except (ValueError, RuntimeError):
        pass

    assert _user_files(_manifest(base_dir)) == before


@pytest.mark.skipif(
    os.name == "nt", reason="creating symlinks needs admin rights on Windows"
)
def test_undo_leaves_symlinks_pointing_at_the_restored_file(test_history_env):
    base_dir, db, _cache, history_manager, _worker = test_history_env
    target = os.path.join(base_dir, "report.txt")
    _write(target, "report")
    link = os.path.join(base_dir, "shortcut.txt")
    os.symlink(target, link)

    session_id = _sort(
        base_dir,
        db,
        history_manager,
        {"Docs": {"report.txt": _entry("../report.txt", "report.txt")}},
    )

    history_manager.rollback(session_id)

    assert _read(target) == "report"
    assert os.path.islink(link)
    assert _read(link) == "report"
