import asyncio
import os
import threading
import pytest

from app.core.mover import AsyncMoveEngine, execute_moves, execute_moves_async


class DummyDB:
    def __init__(self):
        self.doc_store = {}
        self.batch_updates = []
        self.lock = threading.Lock()

    def get_document(self, base_dir, key):
        with self.lock:
            return self.doc_store.get(key, {"file_hash": f"hash_{key}"})

    def execute_batch_updates(self, updates):
        with self.lock:
            self.batch_updates.append(list(updates))

    def set_user_verified_target(self, base_dir, file_hash, dest):
        pass

    def update_document_path(self, base_dir, src, dst):
        pass

    def record_transaction_step(self, *args):
        pass


class DummyHistoryManager:
    def __init__(self):
        self.steps = []
        self.lock = threading.Lock()

    def create_snapshot(self, base_dir):
        return "test-session-123"

    def log_step(self, **kwargs):
        with self.lock:
            self.steps.append(kwargs)

    def clear_step_ledger(self, session_id):
        pass


def test_async_mover_chunked_execution(tmp_path):
    """Test that AsyncMoveEngine executes move plan in bounded chunks via ThreadPoolExecutor."""
    base_dir = str(tmp_path)
    plan = {}
    for i in range(15):
        fname = f"file_{i}.txt"
        fpath = tmp_path / fname
        fpath.write_text(f"content {i}")
        plan[fname] = {
            "__type__": "file",
            "relative_source": fname,
            "target_filename": f"moved_{fname}",
            "status": "Confirmed",
        }

    db = DummyDB()
    hm = DummyHistoryManager()

    engine = AsyncMoveEngine(max_workers=4, chunk_size=5)
    summary = engine.execute(base_dir, plan, db, hm)

    assert not summary.get("cancelled")
    # Verify files were moved
    for i in range(15):
        orig_fname = f"file_{i}.txt"
        moved_fname = f"moved_file_{i}.txt"
        assert not os.path.exists(tmp_path / orig_fname)
        assert os.path.exists(tmp_path / moved_fname)


def test_async_mover_cancellation_interruption(tmp_path):
    """Test that cancellation halts worker scheduling between chunk boundaries."""
    base_dir = str(tmp_path)
    plan = {}
    for i in range(20):
        fname = f"file_{i}.txt"
        fpath = tmp_path / fname
        fpath.write_text(f"content {i}")
        plan[fname] = {
            "__type__": "file",
            "relative_source": fname,
            "target_filename": f"moved_{fname}",
            "status": "Confirmed",
        }

    db = DummyDB()
    hm = DummyHistoryManager()

    def cancel_check():
        if len(hm.steps) >= 5:
            return True
        return False

    summary = execute_moves(
        base_dir, plan, db, hm, chunk_size=5, max_workers=2, cancel_check=cancel_check
    )

    assert summary.get("cancelled") is True
    # First chunk (5 items) should be processed, later chunks should NOT be scheduled
    assert len(hm.steps) >= 5
    assert len(hm.steps) < 20


def test_async_mover_off_thread_hashing(tmp_path, monkeypatch):
    """Test that SHA-256 hash calculation occurs in background worker task off-thread."""
    base_dir = str(tmp_path)
    fname = "hash_test.txt"
    fpath = tmp_path / fname
    fpath.write_text("hash calculation test payload")

    plan = {
        fname: {
            "__type__": "file",
            "relative_source": fname,
            "target_filename": "hash_dest.txt",
            "status": "Confirmed",
        }
    }

    db = DummyDB()
    hm = DummyHistoryManager()

    hashed_threads = []

    from app.core import extractor

    orig_get_file_hash = extractor.get_file_hash

    def mock_get_file_hash(path):
        hashed_threads.append(threading.current_thread().name)
        return orig_get_file_hash(path)

    monkeypatch.setattr("app.core.extractor.get_file_hash", mock_get_file_hash)

    main_thread_name = threading.current_thread().name
    execute_moves(base_dir, plan, db, hm, chunk_size=1)

    assert len(hashed_threads) > 0
    # The hash function must have executed on a worker thread, not the main thread!
    assert hashed_threads[0] != main_thread_name


def test_async_mover_execute_moves_async(tmp_path):
    """Test execute_moves_async coroutine wrapper."""
    base_dir = str(tmp_path)
    fname = "async_coro.txt"
    fpath = tmp_path / fname
    fpath.write_text("async coroutine test")

    plan = {
        fname: {
            "__type__": "file",
            "relative_source": fname,
            "target_filename": "async_dest.txt",
            "status": "Confirmed",
        }
    }

    db = DummyDB()
    hm = DummyHistoryManager()

    summary = asyncio.run(execute_moves_async(base_dir, plan, db, hm))
    assert not summary.get("cancelled")
    assert os.path.exists(tmp_path / "async_dest.txt")
