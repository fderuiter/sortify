import os
import struct
from unittest.mock import patch

import pytest

from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager
from app.core.mover import _remove_empty_dirs, execute_moves, get_safe_path
from app.core.resilient_file_ops import resilient_file_hash


def test_media_parsing_mp3_invalid_boundary(tmp_path):
    # Create an MP3 file with invalid/corrupt ID3 header indicating size past EOF
    mp3_file = tmp_path / "corrupt.mp3"
    # ID3 header with flag and size
    header = b"ID3" + b"\x03\x00\x00" + struct.pack(">I", 99999999)  # Huge size
    mp3_file.write_bytes(header)

    # Calling resilient_file_hash should not hang or crash
    # It should detect invalid boundary/size, abort parsing, and fallback to whole-file hashing
    h = resilient_file_hash(str(mp3_file))
    import hashlib

    expected = hashlib.sha256(header).hexdigest()
    assert h == expected


def test_media_parsing_m4a_negative_box_size(tmp_path):
    # Create an M4A file with a box size that is smaller than the header size, leading to negative seek offset
    m4a_file = tmp_path / "corrupt.m4a"
    # Header box size 2 (which is < 8 header_size)
    header = struct.pack(">I4s", 2, b"mdat")
    m4a_file.write_bytes(header)

    # Calling resilient_file_hash should not hang or crash
    # It should detect negative seek distance, abort parsing, and fallback to whole-file hashing
    h = resilient_file_hash(str(m4a_file))
    import hashlib

    expected = hashlib.sha256(header).hexdigest()
    assert h == expected


def test_symlink_traversal_guard(tmp_path):
    # Create a nested directory structure with a circular folder symlink
    parent_dir = tmp_path / "parent"
    parent_dir.mkdir()

    sub_dir = parent_dir / "subdir"
    sub_dir.mkdir()

    # Create circular symlink: subdir/circ -> parent
    # Skip on Windows if symlink creation is not permitted
    try:
        os.symlink(str(parent_dir), str(sub_dir / "circ"))
    except OSError:
        pytest.skip("Symlinks not supported or requires admin rights")

    # Calling _remove_empty_dirs on parent should not cause infinite recursion crash
    # It should identify and ignore the folder symlink
    _remove_empty_dirs(str(parent_dir))

    # Check that original files and directories still exist (subdir is empty except for symlink,
    # but since symlinks are ignored, subdir itself might or might not be removed depending on implementation.
    # Actually, islink(sub_dir/circ) is True, so when checking os.listdir(sub_dir),
    # "circ" is found. "circ" is a link, so we skip recursing into it.
    # Then listdir(sub_dir) is not empty, so sub_dir is not removed. Parent is not empty, so not removed.
    assert parent_dir.exists()
    assert sub_dir.exists()


def test_collision_loop_limit(tmp_path):
    # Enforce maximum ceiling of 1,000 attempts when generating name suffixes
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    # Mock os.path.lexists to always return True (simulating name conflicts)
    with patch("os.path.lexists", return_value=True):
        with pytest.raises(RuntimeError) as exc_info:
            get_safe_path(str(dest_dir), "file.txt")
        assert "Unique name cannot be resolved within 1,000 attempts" in str(
            exc_info.value
        )


@pytest.fixture
def setup_mover_env(tmp_path):
    base_dir = tmp_path / "mover_base"
    base_dir.mkdir()

    db_worker = DBWorker()
    db_path = tmp_path / "mover_db.db"
    db = Database(db_path, worker=db_worker)

    cache_path = tmp_path / "mover_cache.db"
    from app.core.cache import CacheManager

    cache = CacheManager(str(cache_path), worker=db_worker)

    history_manager = HistoryManager(db, cache, str(tmp_path / "mover_history.db"))

    yield base_dir, db, history_manager, db_worker
    db_worker.stop()


def test_transactional_sync_execution_on_failure(setup_mover_env):
    base_dir, db, history_manager, db_worker = setup_mover_env

    # Initialize some document in the DB
    src_file = base_dir / "test.txt"
    src_file.write_text("content")

    db.execute_batch_updates(
        [{"type": "verified_target", "args": (str(base_dir), "hash123", "")}]
    )
    # Setup document
    from app.core.db_conn import get_db_connection

    conn = get_db_connection(db.db_path)
    with conn:
        conn.execute(
            "INSERT INTO documents (base_dir, filepath, file_hash, extracted_text) VALUES (?, ?, ?, ?)",
            (
                str(base_dir),
                "test.txt",
                "hash123",
                db.crypto.encrypt_text("extracted_text"),
            ),
        )

    # Create plan to move test.txt to dest.txt
    plan = {
        "test.txt": {
            "__type__": "file",
            "relative_source": "test.txt",
            "target_filename": "dest.txt",
            "file_hash": "hash123",
        }
    }

    # Mock resilient_move to raise OSError (simulating write/move failure)
    with patch(
        "app.core.resilient_file_ops.resilient_move",
        side_effect=OSError("Write failure"),
    ):
        with pytest.raises(OSError):
            execute_moves(str(base_dir), plan, db, history_manager)

    # Verify that the database record has NOT been updated and still points to "test.txt"
    doc = db.get_document(str(base_dir), "test.txt")
    assert doc is not None
    assert doc["file_hash"] == "hash123"

    # Confirm that no document at "dest.txt" was updated or inserted
    doc_dest = db.get_document(str(base_dir), "dest.txt")
    assert doc_dest is None


def test_active_rollback_bypasses_batch_updates(setup_mover_env):
    base_dir, db, history_manager, db_worker = setup_mover_env

    # Set active_rollback to True
    db.active_rollback = True

    # Attempting to execute batch updates should be bypassed
    updates = [
        {"type": "document_path", "args": (str(base_dir), "test.txt", "dest.txt")}
    ]
    db.execute_batch_updates(updates)

    # Verify no document was added/modified
    doc = db.get_document(str(base_dir), "dest.txt")
    assert doc is None
