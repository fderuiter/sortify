import os
import threading

import pytest

from app.core.mover import (
    _VALID_MOVE_ENGINE_TRANSITIONS,
    AsyncMoveEngine,
    DirectoryCleanupStatus,
    InvalidMoveStateError,
    MoveEngineState,
    MoveOpState,
    MoveResult,
    MoveVerificationStatus,
    _process_move_item,
    execute_moves,
)


class DummyDB:
    def __init__(self):
        self.doc_store = {}
        self.batch_updates = []
        self.lock = threading.Lock()
        self.get_document_calls = []
        self.get_document_hashes_calls = []

    def get_document(self, base_dir, key):
        with self.lock:
            self.get_document_calls.append(
                (threading.current_thread().name, base_dir, key)
            )
            return self.doc_store.get(key, {"file_hash": f"hash_{key}"})

    def get_document_hashes(self, base_dir, filepaths):
        with self.lock:
            self.get_document_hashes_calls.append(
                (threading.current_thread().name, base_dir, list(filepaths))
            )
            return {
                fp: self.doc_store.get(fp, {}).get("file_hash", f"hash_{fp}")
                for fp in filepaths
            }

    def execute_batch_updates(self, updates):
        with self.lock:
            self.batch_updates.append(list(updates))

    def set_user_verified_target(self, base_dir, file_hash, dest):
        pass

    def update_document_path(self, base_dir, src, dst):
        pass


class DummyMoveLedger:
    def __init__(self, steps: list):
        self._steps = steps
        self._lock = threading.Lock()

    def record_intent(self, session_id, src, dst, src_sha256=None, size=None):
        with self._lock:
            self._steps.append((session_id, src, dst))
            return len(self._steps)

    def record_displaced(self, session_id, seq, displaced):
        pass

    def mark_done(self, session_id, seq, dst=None, displaced=None):
        pass

    def mark_verified(self, session_id, seq):
        pass

    def mark_failed(self, session_id, seq, error=None):
        pass


class DummyHistoryManager:
    def __init__(self):
        self.steps = []
        self.moves = DummyMoveLedger(self.steps)

    def start_session(self, base_dir):
        return "test-session-123"

    def complete_session(self, session_id):
        pass

    def unwind_session(self, session_id, db=None):
        return len(self.steps)


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


def test_async_mover_progress_callback_propagation(tmp_path):
    """Test that AsyncMoveEngine emits progress updates per completed chunk."""
    from app.core.progress import ProgressUpdate

    base_dir = str(tmp_path)
    plan = {}
    for i in range(10):
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

    progress_updates = []

    def progress_cb(update: ProgressUpdate):
        progress_updates.append(update)

    summary = execute_moves(
        base_dir, plan, db, hm, chunk_size=2, progress_callback=progress_cb
    )

    assert not summary.get("cancelled")
    assert len(progress_updates) == 5
    for idx, up in enumerate(progress_updates):
        expected_ratio = (idx + 1) / 5.0
        assert abs(up.progress - expected_ratio) < 1e-5
        assert up.unit_type == "files"
        assert up.unit_count == (idx + 1) * 2


def test_move_engine_initial_state_and_lifecycle_transitions(tmp_path):
    """Test that AsyncMoveEngine starts in IDLE and transitions through valid lifecycle states."""
    engine = AsyncMoveEngine(max_workers=2, chunk_size=2)
    assert engine.state == MoveEngineState.IDLE

    # Transition to PREPARING
    engine.transition_to(MoveEngineState.PREPARING)
    assert engine.state == MoveEngineState.PREPARING

    # Transition to MOVING_CHUNK
    engine.transition_to(MoveEngineState.MOVING_CHUNK)
    assert engine.state == MoveEngineState.MOVING_CHUNK

    # Self-transition on subsequent chunk
    engine.transition_to(MoveEngineState.MOVING_CHUNK)
    assert engine.state == MoveEngineState.MOVING_CHUNK

    # Transition to RESOLVING_LINKS
    engine.transition_to(MoveEngineState.RESOLVING_LINKS)
    assert engine.state == MoveEngineState.RESOLVING_LINKS

    # Transition to CLEANING_DIRS
    engine.transition_to(MoveEngineState.CLEANING_DIRS)
    assert engine.state == MoveEngineState.CLEANING_DIRS

    # Transition to COMPLETED
    engine.transition_to(MoveEngineState.COMPLETED)
    assert engine.state == MoveEngineState.COMPLETED

    # Reset back to IDLE
    engine.reset()
    assert engine.state == MoveEngineState.IDLE


def test_move_engine_invalid_transitions_raise_error():
    """Test that illegal lifecycle transitions immediately raise InvalidMoveStateError."""
    engine = AsyncMoveEngine()
    assert engine.state == MoveEngineState.IDLE

    # IDLE cannot transition directly to COMPLETED or MOVING_CHUNK
    with pytest.raises(InvalidMoveStateError, match="Illegal move engine transition"):
        engine.transition_to(MoveEngineState.COMPLETED)

    with pytest.raises(InvalidMoveStateError, match="Illegal move engine transition"):
        engine.transition_to(MoveEngineState.MOVING_CHUNK)

    with pytest.raises(InvalidMoveStateError, match="Illegal move engine transition"):
        engine.transition_to(MoveEngineState.ROLLED_BACK)

    # Transition to PREPARING
    engine.transition_to(MoveEngineState.PREPARING)

    # Cannot jump to ROLLED_BACK directly
    with pytest.raises(InvalidMoveStateError):
        engine.transition_to(MoveEngineState.ROLLED_BACK)

    # Unknown state string
    with pytest.raises(InvalidMoveStateError, match="Unknown or invalid target state"):
        engine.transition_to("NONEXISTENT_STATE")


def test_move_engine_reentrant_lock():
    """Test that engine state transitions are protected by a re-entrant lock (RLock)."""
    engine = AsyncMoveEngine()
    assert isinstance(engine._state_lock, type(threading.RLock()))

    # Re-entrant acquisition from the same thread does not deadlock
    with engine._state_lock:
        engine.transition_to(MoveEngineState.PREPARING)
        assert engine.state == MoveEngineState.PREPARING

    # Concurrent state queries and transitions across threads
    threads = []
    errors = []

    def read_worker():
        try:
            for _ in range(50):
                _ = engine.state
        except Exception as exc:
            errors.append(exc)

    for _ in range(4):
        t = threading.Thread(target=read_worker)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    assert not errors


def test_move_engine_cancellation_transitions(tmp_path):
    """Test that cancellation transitions engine from MOVING_CHUNK to CANCELLING and CANCELLED."""
    base_dir = str(tmp_path)
    plan = {}
    for i in range(10):
        fname = f"file_{i}.txt"
        (tmp_path / fname).write_text(f"content {i}")
        plan[fname] = {
            "__type__": "file",
            "relative_source": fname,
            "target_filename": f"out_{fname}",
            "status": "Confirmed",
        }

    db = DummyDB()
    hm = DummyHistoryManager()

    engine = AsyncMoveEngine(max_workers=1, chunk_size=2)

    def cancel_check():
        return True

    res = engine.execute(base_dir, plan, db, hm, cancel_check=cancel_check)
    assert res.cancelled is True
    assert res["cancelled"] is True
    assert res.state == MoveEngineState.CANCELLED
    assert engine.state == MoveEngineState.CANCELLED


def test_move_engine_exception_rollback_transitions(tmp_path):
    """Test that unhandled chunk exceptions trigger FAILED, ROLLING_BACK, and ROLLED_BACK."""
    base_dir = str(tmp_path)
    fname = "err_file.txt"
    (tmp_path / fname).write_text("hello")
    plan = {
        fname: {
            "__type__": "file",
            "relative_source": fname,
            "target_filename": "dest_err.txt",
            "status": "Confirmed",
        }
    }

    db = DummyDB()

    class RollbackHistoryManager:
        def __init__(self):
            self.unwound = False

        def start_session(self, base):
            return "snap-123"

        def unwind_session(self, session_id, db=None):
            self.unwound = True

    hm = RollbackHistoryManager()
    engine = AsyncMoveEngine(max_workers=1, chunk_size=1)

    # Force an exception during execute by making pool.submit raise
    from unittest.mock import patch

    with patch(
        "app.core.mover._process_move_item", side_effect=RuntimeError("Disk failure")
    ):
        with pytest.raises(RuntimeError, match="Disk failure"):
            engine.execute(base_dir, plan, db, hm)

    assert hm.unwound is True
    assert engine.state == MoveEngineState.ROLLED_BACK


def test_move_result_dictionary_compatibility_and_boolean_evaluation():
    """Test MoveResult subscripting, .get(), containment, attribute access, and boolean evaluation."""
    res = MoveResult(
        session_id="session-456",
        deleted_folders=2,
        protected_folders=1,
        cancelled=False,
        state=MoveEngineState.COMPLETED,
    )

    # Dictionary inheritance
    assert isinstance(res, dict)
    assert len(res) >= 4

    # Subscript access
    assert res["session_id"] == "session-456"
    assert res["deleted_folders"] == 2
    assert res["protected_folders"] == 1
    assert res["cancelled"] is False
    assert res["state"] == MoveEngineState.COMPLETED

    # Subscript assignment
    res["deleted_folders"] = 3
    assert res["deleted_folders"] == 3

    # .get() with defaults
    assert res.get("session_id") == "session-456"
    assert res.get("missing_key") is None
    assert res.get("missing_key", "default_val") == "default_val"

    # Containment
    assert "session_id" in res
    assert "cancelled" in res
    assert "nonexistent" not in res

    # Attribute access
    assert res.session_id == "session-456"
    assert res.deleted_folders == 3
    assert res.protected_folders == 1
    assert res.cancelled is False
    assert res.state == MoveEngineState.COMPLETED

    # Attribute mutation reflects in dict
    res.cancelled = True
    assert res["cancelled"] is True
    assert res.cancelled is True

    # Boolean evaluation: res evaluates truthy because it's a non-empty result
    assert bool(res) is True

    # Boolean status evaluation of cancelled
    assert bool(res["cancelled"]) is True
    assert bool(res.get("cancelled")) is True

    # Result ok property
    assert res.ok is False
    res.cancelled = False
    assert res.ok is True

    res.state = MoveEngineState.FAILED
    assert res.ok is False


def test_move_result_pipeline_compatibility():
    """Verify MoveResult works seamlessly with SortResult.from_summary."""
    from app.core.pipeline import SortResult

    res = MoveResult(
        session_id="session-789",
        cancelled=False,
        state=MoveEngineState.COMPLETED,
        audit_report={
            "records": [
                {
                    "source_path": "a.txt",
                    "destination_path": "sorted/a.txt",
                    "status": "VERIFIED_PASS",
                }
            ]
        },
    )

    sort_res = SortResult.from_summary(res, quarantine_dir="quarantine")
    assert sort_res.session_id == "session-789"
    assert sort_res.cancelled is False
    assert sort_res.ok is True
    assert "a.txt" in sort_res.moved


def test_typed_status_enumerations():
    """Verify all required status enum members and transitions table."""
    # MoveOpState
    assert MoveOpState.INTENT == "intent"
    assert MoveOpState.DISPLACEMENT == "displacement"
    assert MoveOpState.DONE == "done"
    assert MoveOpState.VERIFIED == "verified"
    assert MoveOpState.FAILED == "failed"

    # DirectoryCleanupStatus
    assert DirectoryCleanupStatus.TO_BE_DELETED == "To Be Deleted"
    assert DirectoryCleanupStatus.PROTECTED == "Protected"

    # MoveVerificationStatus
    assert MoveVerificationStatus.VERIFIED_PASS == "VERIFIED_PASS"
    assert MoveVerificationStatus.MISMATCH_FAIL == "MISMATCH_FAIL"
    assert MoveVerificationStatus.MOVE_FAIL == "MOVE_FAIL"

    # _VALID_MOVE_ENGINE_TRANSITIONS coverage
    all_states = set(MoveEngineState)
    assert len(all_states) == 11
    assert set(_VALID_MOVE_ENGINE_TRANSITIONS.keys()) == all_states


def test_batch_prefetch_file_hashes_no_worker_lock_contention(tmp_path):
    """Verify batch prefetch happens on caller thread and worker threads do not call get_document."""
    base_dir = str(tmp_path)
    plan = {}
    for i in range(12):
        fname = f"file_{i}.txt"
        fpath = tmp_path / fname
        fpath.write_text(f"content {i}")
        plan[fname] = {
            "__type__": "file",
            "relative_source": fname,
            "target_filename": f"sorted_{fname}",
            "status": "Confirmed",
        }

    db = DummyDB()
    # Populate specific hashes for verification
    for i in range(12):
        fname = f"file_{i}.txt"
        db.doc_store[fname] = {"file_hash": f"precomputed_hash_{i}"}

    hm = DummyHistoryManager()

    engine = AsyncMoveEngine(max_workers=4, chunk_size=4)
    summary = engine.execute(base_dir, plan, db, hm)

    assert not summary.get("cancelled")
    # 12 items / chunk_size 4 = 3 chunks -> 3 batch hash prefetch calls
    assert len(db.get_document_hashes_calls) == 3
    for call_thread, b_dir, paths in db.get_document_hashes_calls:
        assert len(paths) == 4
        assert b_dir == base_dir

    # Zero worker threads called get_document! Lock contention eliminated.
    assert len(db.get_document_calls) == 0

    # Verify all files moved
    for i in range(12):
        fname = f"file_{i}.txt"
        assert not os.path.exists(tmp_path / fname)
        assert os.path.exists(tmp_path / f"sorted_{fname}")

    # Verify verified_target batch updates received the precomputed hashes
    all_batch_args = []
    for batch in db.batch_updates:
        for update in batch:
            if update.get("type") == "verified_target":
                all_batch_args.append(update["args"])

    assert len(all_batch_args) == 12
    recorded_hashes = {args[1] for args in all_batch_args}
    for i in range(12):
        assert f"precomputed_hash_{i}" in recorded_hashes


def test_batch_prefetch_missing_document_fallback(tmp_path):
    """Verify non-existent documents fallback gracefully to empty hash strings."""
    base_dir = str(tmp_path)
    fname = "unindexed.txt"
    fpath = tmp_path / fname
    fpath.write_text("unindexed content")

    plan = {
        fname: {
            "__type__": "file",
            "relative_source": fname,
            "target_filename": "moved_unindexed.txt",
            "status": "Confirmed",
        }
    }

    db = DummyDB()
    # Explicitly return empty string for unindexed document
    db.doc_store[fname] = {"file_hash": ""}

    hm = DummyHistoryManager()
    engine = AsyncMoveEngine(max_workers=2, chunk_size=2)
    summary = engine.execute(base_dir, plan, db, hm)

    assert not summary.get("cancelled")
    assert os.path.exists(tmp_path / "moved_unindexed.txt")


def test_process_move_item_legacy_caller_fallback(tmp_path):
    """Verify backward compatibility: if chunk_hash_map is not provided, falls back to get_document."""
    base_dir = str(tmp_path)
    fname = "legacy.txt"
    fpath = tmp_path / fname
    fpath.write_text("legacy caller test")

    item = {
        "source_path": str(fpath),
        "key": fname,
        "content": {},
        "filename": fname,
        "current_dest": "legacy_dest.txt",
    }

    db = DummyDB()
    db.doc_store[fname] = {"file_hash": "legacy_hash_123"}
    hm = DummyHistoryManager()
    db_updates = []
    lock = threading.Lock()

    # Call _process_move_item directly without chunk_hash_map argument
    success = _process_move_item(
        base_dir=base_dir,
        item=item,
        db=db,
        path_map={},
        runtime_settings=None,
        session_id="test_legacy_session",
        history_manager=hm,
        db_lock=lock,
        db_updates_batch=db_updates,
    )

    assert success is True
    # Verify fallback called db.get_document
    assert len(db.get_document_calls) == 1
    assert db.get_document_calls[0][2] == fname
    assert os.path.exists(tmp_path / "legacy_dest.txt")


def test_database_get_document_hashes_sqlite(tmp_path, monkeypatch):
    """Verify Database.get_document_hashes queries filepath and file_hash without text decryption."""
    from app.core.db import Database
    from app.core.db_worker import DBWorker

    db_worker = DBWorker()
    try:
        db_path = tmp_path / "test_hashes.db"
        db = Database(db_path, worker=db_worker)
        base_dir = str(tmp_path / "test_base")
        os.makedirs(base_dir, exist_ok=True)

        # Insert 3 documents with extracted text
        for i in range(3):
            db.upsert_document(
                base_dir=base_dir,
                filepath=f"sub/doc_{i}.txt",
                file_hash=f"hash_{i}",
                extracted_text=f"Sample extracted text for doc {i}",
            )

        # Monkeypatch crypto.decrypt_text to ensure it is NOT called during get_document_hashes
        decrypt_calls = []
        orig_decrypt = db.crypto.decrypt_text

        def mock_decrypt(text):
            decrypt_calls.append(text)
            return orig_decrypt(text)

        monkeypatch.setattr(db.crypto, "decrypt_text", mock_decrypt)

        # Call get_document_hashes for existing and missing paths
        paths_to_query = [
            "sub/doc_0.txt",
            "sub\\doc_1.txt",  # Windows style path
            "sub/doc_2.txt",
            "nonexistent.txt",
        ]

        hashes = db.get_document_hashes(base_dir, paths_to_query)

        # Decryption should NOT be called at all
        assert len(decrypt_calls) == 0

        # Existing files return correct hashes
        assert hashes["sub/doc_0.txt"] == "hash_0"
        assert hashes["sub\\doc_1.txt"] == "hash_1"
        assert hashes["sub/doc_1.txt"] == "hash_1"
        assert hashes["sub/doc_2.txt"] == "hash_2"

        # Nonexistent file returns empty string
        assert hashes["nonexistent.txt"] == ""
    finally:
        db_worker.stop()
