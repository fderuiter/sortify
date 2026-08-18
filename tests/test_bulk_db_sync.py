import os
from unittest.mock import MagicMock

import pytest

from app.core.mover import execute_moves


class MockDB:
    def __init__(self):
        self.updates = []
        self.execute_batch_updates_called = 0
        self.last_batch = []
        self.doc_store = {}

    def get_document(self, base_dir, key):
        return self.doc_store.get(key, {"file_hash": f"hash_{key}"})

    def execute_batch_updates(self, updates):
        self.execute_batch_updates_called += 1
        self.last_batch = updates
        self.updates.extend(updates)


class MockHistoryManager:
    def create_snapshot(self, base_dir):
        return "snap-123"


@pytest.fixture
def db():
    return MockDB()


@pytest.fixture
def history_manager():
    return MockHistoryManager()


def test_successful_folder_relocation(tmp_path, db, history_manager, monkeypatch):
    """
    Scenario: Successful Folder Relocation
    Ensure that execute_batch_updates is called EXACTLY ONCE with all updates
    after physical operations complete.
    """
    base_dir = str(tmp_path)
    file1 = tmp_path / "file1.txt"
    file2 = tmp_path / "file2.txt"
    file1.write_text("a")
    file2.write_text("b")

    plan = {
        "target_dir": {
            "file1.txt": {
                "__type__": "file",
                "relative_source": "../file1.txt",
                "target_filename": "file1.txt",
            },
            "file2.txt": {
                "__type__": "file",
                "relative_source": "../file2.txt",
                "target_filename": "file2.txt",
            },
        }
    }

    # Mock shutil.move so we don't actually move files and mess up tests but can track calls
    move_mock = MagicMock()
    monkeypatch.setattr("app.core.mover.shutil.move", move_mock)

    execute_moves(base_dir, plan, db, history_manager)

    # Shutil move should be called twice
    assert move_mock.call_count == 2

    # execute_batch_updates should be called exactly once
    assert db.execute_batch_updates_called == 1

    # Should have 4 updates (verified_target + document_path for each file)
    assert len(db.last_batch) == 4
    types = [u["type"] for u in db.last_batch]
    assert types.count("verified_target") == 2
    assert types.count("document_path") == 2


def test_interrupted_folder_relocation(tmp_path, db, history_manager, monkeypatch):
    """
    Scenario: Interrupted Folder Relocation
    If an individual physical move fails, the system must exclude that file from the
    final database update batch, and execute updates for files that succeeded prior to the error.
    """
    base_dir = str(tmp_path)
    file1 = tmp_path / "file1.txt"
    file2 = tmp_path / "file2.txt"
    file1.write_text("a")
    file2.write_text("b")

    plan = {
        "target_dir": {
            "file1.txt": {
                "__type__": "file",
                "relative_source": "../file1.txt",
                "target_filename": "file1.txt",
            },
            "file2.txt": {
                "__type__": "file",
                "relative_source": "../file2.txt",
                "target_filename": "file2.txt",
            },
        }
    }

    move_calls = []

    def mock_move(src, dst):
        move_calls.append((src, dst))
        if "file2.txt" in src:
            raise OSError("Disk full")

    monkeypatch.setattr("app.core.mover.shutil.move", mock_move)

    with pytest.raises(OSError, match="Disk full"):
        execute_moves(base_dir, plan, db, history_manager)

    # Shutil move should be called once or twice depending on dictionary order,
    # but the batch should only contain updates for files processed BEFORE the error.
    assert len(move_calls) > 0
    assert "file2.txt" in move_calls[-1][0]

    # execute_batch_updates should be called exactly once in the exception handler
    assert db.execute_batch_updates_called == 1

    # Since file2 failed, its document_path AND verified_target updates will NOT be in the batch.
    # The batch should only contain updates for file1 (which succeeded).
    assert len(db.last_batch) == 2
    for item in db.last_batch:
        assert "file1.txt" in str(item["args"])
        assert "file2.txt" not in str(item["args"])


def test_cleanup_ordering(tmp_path, db, history_manager, monkeypatch):
    """
    Scenario: Directory Cleanup Ordering
    Ensure that os.rmdir executes BEFORE the final db.execute_batch_updates.
    """
    base_dir = str(tmp_path)
    # Create empty directory
    empty_dir = tmp_path / "empty_dir"
    empty_dir.mkdir()

    plan = {
        "empty_dir": {
            "__type__": "directory",
            "source_path": str(empty_dir),
            "status": "To Be Deleted",
        }
    }

    call_order = []

    original_rmdir = os.rmdir

    def mock_rmdir(path):
        call_order.append("rmdir")
        original_rmdir(path)

    monkeypatch.setattr(os, "rmdir", mock_rmdir)

    original_execute = db.execute_batch_updates

    def mock_execute(updates):
        call_order.append("db_update")
        original_execute(updates)

    db.execute_batch_updates = mock_execute

    execute_moves(base_dir, plan, db, history_manager)

    # ensure order is rmdir followed by db_update
    assert call_order == ["rmdir", "db_update"]


def test_chunked_batching_120_files(tmp_path, db, history_manager, monkeypatch):
    """
    Scenario: Moving 120 files triggers chunked DB flushes
    Moving 120 files should trigger execute_batch_updates 3 times:
    - 50 files (100 update items)
    - 50 files (100 update items)
    - 20 files (40 update items)
    """
    base_dir = str(tmp_path)
    target_dir_dict = {}

    for i in range(120):
        fname = f"file_{i:03d}.txt"
        fpath = tmp_path / fname
        fpath.write_text(f"content_{i}")
        target_dir_dict[fname] = {
            "__type__": "file",
            "relative_source": f"../{fname}",
            "target_filename": fname,
        }

    plan = {"target_dir": target_dir_dict}

    batch_history = []

    original_execute = db.execute_batch_updates

    def mock_execute(updates):
        batch_history.append(list(updates))
        original_execute(updates)

    db.execute_batch_updates = mock_execute

    move_mock = MagicMock()
    monkeypatch.setattr("app.core.mover.shutil.move", move_mock)

    execute_moves(base_dir, plan, db, history_manager)

    assert move_mock.call_count == 120
    assert db.execute_batch_updates_called == 3
    assert len(batch_history) == 3

    # Check batch sizes in terms of items (2 items per file: verified_target + document_path)
    assert len(batch_history[0]) == 100
    assert len(batch_history[1]) == 100
    assert len(batch_history[2]) == 40
    assert len(db.updates) == 240


def test_interrupted_large_move_preserves_committed_chunks(
    tmp_path, db, history_manager, monkeypatch
):
    """
    Scenario: Interrupted large move preserves committed chunks and pre-failure files.
    When moving 60 files and file 55 fails:
    - Chunk 1 (50 files) was committed during traversal.
    - Files 51-54 are committed in exception handler.
    - File 55 is excluded.
    """
    base_dir = str(tmp_path)
    target_dir_dict = {}

    # Sort keys numerically so iteration order is predictable file_000 to file_059
    for i in range(60):
        fname = f"file_{i:03d}.txt"
        fpath = tmp_path / fname
        fpath.write_text(f"content_{i}")
        target_dir_dict[fname] = {
            "__type__": "file",
            "relative_source": f"../{fname}",
            "target_filename": fname,
        }

    plan = {"target_dir": target_dir_dict}

    def mock_move(src, dst):
        if "file_055.txt" in src:
            raise OSError("Disk failure mid-operation")

    monkeypatch.setattr("app.core.mover.shutil.move", mock_move)

    with pytest.raises(OSError, match="Disk failure mid-operation"):
        execute_moves(base_dir, plan, db, history_manager)

    # execute_batch_updates called twice: once for chunk 1 (50 files) and once in exception handler (4 files)
    assert db.execute_batch_updates_called == 2

    # Check committed updates
    committed_args = [str(u["args"]) for u in db.updates]
    # Check that file_000 to file_054 are in committed updates
    for i in range(55):
        fname = f"file_{i:03d}.txt"
        assert any(fname in args for args in committed_args)

    # Check that file_055.txt is NOT in committed updates
    assert not any("file_055.txt" in args for args in committed_args)


def test_cache_invalidation_on_chunk_flush(
    tmp_path, history_manager, monkeypatch
):
    """
    Scenario: Document path cache clears immediately following each completed database commit chunk.
    Verify that Database.invalidate_cache is called during each chunk flush.
    """
    from app.core.db import Database
    from app.core.db_worker import DBWorker

    db_file = tmp_path / "test.db"
    worker = DBWorker()
    db = Database(db_file, worker)

    # Populate dummy documents
    target_dir_dict = {}
    docs_to_insert = []
    for i in range(60):
        fname = f"file_{i:03d}.txt"
        fpath = tmp_path / fname
        fpath.write_text(f"content_{i}")
        docs_to_insert.append((str(tmp_path), fname, f"hash_{i}", f"content_{i}"))
        target_dir_dict[fname] = {
            "__type__": "file",
            "relative_source": f"../{fname}",
            "target_filename": fname,
        }

    db.upsert_documents(docs_to_insert)

    plan = {"target_dir": target_dir_dict}

    invalidate_calls = []
    original_invalidate = db.invalidate_cache

    def mock_invalidate():
        invalidate_calls.append(True)
        original_invalidate()

    monkeypatch.setattr(db, "invalidate_cache", mock_invalidate)

    move_mock = MagicMock()
    monkeypatch.setattr("app.core.mover.shutil.move", move_mock)

    execute_moves(str(tmp_path), plan, db, history_manager)

    # invalidate_cache is called when execute_batch_updates is executed for chunk 1 (50 files) and chunk 2 (10 files)
    assert len(invalidate_calls) >= 2
    worker.stop()


