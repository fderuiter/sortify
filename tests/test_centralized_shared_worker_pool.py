import socket
import threading
import time

import pytest

from app.core.db_worker import DBWorker
from app.core.mover import execute_moves
from app.core.shared_registry import SharedWorkerPool


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
        return "test-session-centralized"

    def log_step(self, **kwargs):
        with self.lock:
            self.steps.append(kwargs)

    def clear_step_ledger(self, session_id):
        pass


def test_db_worker_background_job_routes_through_shared_pool():
    """Verify DBWorker.submit_background_job executes tasks via SharedWorkerPool."""
    worker = DBWorker()
    executed_threads = []
    sandboxed_states = []

    def background_task(arg):
        from app.core.shared_registry import _thread_local
        executed_threads.append(threading.current_thread().name)
        sandboxed_states.append(getattr(_thread_local, "sandboxed", False))
        return f"result_{arg}"

    try:
        fut = worker.submit_background_job(background_task, "test")
        assert fut is not None
        res = fut.result(timeout=5)
        assert res == "result_test"
        assert len(executed_threads) == 1
        assert executed_threads[0].startswith("GlobalSharedWorker")
        assert sandboxed_states[0] is True
    finally:
        worker.stop()


def test_db_worker_background_job_sandboxes_socket():
    """Verify DBWorker background jobs enforce socket sandboxing via SharedWorkerPool."""
    worker = DBWorker()

    def network_task():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("8.8.8.8", 53))

    try:
        fut = worker.submit_background_job(network_task)
        assert fut is not None
        with pytest.raises(PermissionError) as exc_info:
            fut.result(timeout=5)
        assert "External network connections are blocked" in str(exc_info.value)
    finally:
        worker.stop()


def test_mover_chunked_relocation_routes_through_shared_pool(tmp_path):
    """Verify mover file relocation executes chunks via SharedWorkerPool threads."""
    base_dir = str(tmp_path)
    plan = {}
    for i in range(10):
        fname = f"item_{i}.txt"
        fpath = tmp_path / fname
        fpath.write_text(f"content {i}")
        plan[fname] = {
            "__type__": "file",
            "relative_source": fname,
            "target_filename": f"relocated_{fname}",
            "status": "Confirmed",
        }

    db = DummyDB()
    hm = DummyHistoryManager()

    executed_threads = set()
    from app.core import mover
    orig_process = mover._process_move_item

    def mock_process_move_item(*args, **kwargs):
        executed_threads.add(threading.current_thread().name)
        return orig_process(*args, **kwargs)

    pytest.MonkeyPatch().setattr(mover, "_process_move_item", mock_process_move_item)

    summary = execute_moves(base_dir, plan, db, hm, chunk_size=3)
    assert not summary.get("cancelled")

    # Ensure tasks were executed on GlobalSharedWorker threads
    assert len(executed_threads) > 0
    for thread_name in executed_threads:
        assert thread_name.startswith("GlobalSharedWorker")


def test_concurrent_high_load_thread_bound(tmp_path):
    """Verify total background worker thread count remains within global pool limits under load."""
    pool = SharedWorkerPool.get_instance()
    max_workers = pool.max_workers

    active_thread_names = set()
    lock = threading.Lock()

    def heavy_task(idx):
        with lock:
            active_thread_names.add(threading.current_thread().name)
        time.sleep(0.05)
        return idx

    db_worker = DBWorker()
    try:
        futures = []
        # Submit 20 simultaneous background tasks
        for i in range(10):
            futures.append(db_worker.submit_background_job(heavy_task, i))
            futures.append(pool.submit(heavy_task, i + 100))

        results = [f.result(timeout=10) for f in futures if f is not None]
        assert len(results) == 20

        # Check that thread names starting with GlobalSharedWorker do not exceed max_workers
        shared_worker_threads = {t for t in active_thread_names if t.startswith("GlobalSharedWorker")}
        assert len(shared_worker_threads) <= max_workers
    finally:
        db_worker.stop()
