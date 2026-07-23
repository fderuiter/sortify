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
            "file1.txt": {"__type__": "file", "target_filename": "file1.txt"},
            "file2.txt": {"__type__": "file", "target_filename": "file2.txt"}
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
    types = [u['type'] for u in db.last_batch]
    assert types.count('verified_target') == 2
    assert types.count('document_path') == 2


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
            "file1.txt": {"__type__": "file", "target_filename": "file1.txt"},
            "file2.txt": {"__type__": "file", "target_filename": "file2.txt"}
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
        assert "file1.txt" in str(item['args'])
        assert "file2.txt" not in str(item['args'])


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
