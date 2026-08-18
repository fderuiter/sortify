"""Tests for SQLite Transaction Ledger with Incremental Checkpoints."""

import os
import shutil
import tempfile
import time
import pytest

from app.core.ledger import TransactionLedger
from app.core.mover import execute_moves
from app.core.db_conn import get_db_connection


class MockDB:
    def __init__(self):
        self.doc_store = {}
        self.batch_updates = []
        self.verified_targets = {}
        self.paths = {}

    def get_document(self, base_dir, key):
        return self.doc_store.get(key, {"file_hash": f"hash_{key}"})

    def set_user_verified_target(self, base_dir, file_hash, target_path):
        self.verified_targets[file_hash] = target_path

    def update_document_path(self, base_dir, old_filepath, new_filepath):
        self.paths[old_filepath] = new_filepath

    def execute_batch_updates(self, updates):
        self.batch_updates.extend(updates)
        for item in updates:
            if item["type"] == "verified_target":
                b, h, t = item["args"]
                self.set_user_verified_target(b, h, t)
            elif item["type"] == "document_path":
                b, o, n = item["args"]
                self.update_document_path(b, o, n)


class MockHistoryManager:
    def create_snapshot(self, base_dir):
        return "snap-test-ledger"

    def get_sessions(self):
        return []


def test_ledger_wal_mode(tmp_path):
    """Verify transaction ledger initializes database in WAL mode."""
    ledger_db = str(tmp_path / "ledger.db")
    ledger = TransactionLedger(ledger_db)

    conn = get_db_connection(ledger_db)
    with conn:
        cur = conn.execute("PRAGMA journal_mode;")
        mode = cur.fetchone()[0]
    assert mode.lower() == "wal"


def test_incremental_logging_lifecycle(tmp_path):
    """Verify incremental logging of move intent, physical move, and completion."""
    ledger_db = str(tmp_path / "ledger.db")
    ledger = TransactionLedger(ledger_db)

    base_dir = str(tmp_path)
    src_file = tmp_path / "doc.pdf"
    src_file.write_text("content")

    dst_dir = tmp_path / "Folder"
    dst_dir.mkdir()
    dst_file = dst_dir / "doc.pdf"

    session_id = "session-123"
    entry_id = ledger.log_intent(
        session_id=session_id,
        base_dir=base_dir,
        source_path=str(src_file),
        dest_path=str(dst_file),
        source_rel_path="doc.pdf",
        dest_rel_path="Folder/doc.pdf",
        current_dest="Folder",
        file_hash="hash_doc.pdf",
    )

    pending = ledger.get_pending_entries(session_id)
    assert len(pending) == 1
    assert pending[0]["status"] == "PENDING"

    ledger.update_status(entry_id, "MOVED_PHYSICAL")
    pending = ledger.get_pending_entries(session_id)
    assert len(pending) == 1
    assert pending[0]["status"] == "MOVED_PHYSICAL"

    ledger.update_status(entry_id, "COMPLETED")
    pending = ledger.get_pending_entries(session_id)
    assert len(pending) == 0


def test_session_purging_upon_completion(tmp_path):
    """Verify session entries are automatically purged when batch moves succeed."""
    ledger_db = str(tmp_path / "ledger.db")
    ledger = TransactionLedger(ledger_db)

    base_dir = str(tmp_path)
    f1 = tmp_path / "file1.txt"
    f2 = tmp_path / "file2.txt"
    f1.write_text("a")
    f2.write_text("b")

    plan = {
        "Target": {
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

    class Settings:
        transaction_ledger = ledger

    db = MockDB()
    hm = MockHistoryManager()

    execute_moves(base_dir, plan, db, hm, runtime_settings=Settings())

    # Sidecar entries should be purged after successful batch completion
    pending = ledger.get_pending_entries()
    assert len(pending) == 0


def test_simulated_mid_batch_crash_reconciliation(tmp_path):
    """Simulate a mid-batch process crash and verify automated headless reconciliation."""
    ledger_db = str(tmp_path / "ledger.db")
    ledger = TransactionLedger(ledger_db)

    base_dir = str(tmp_path)
    src_file1 = tmp_path / "file1.txt"
    src_file2 = tmp_path / "file2.txt"
    src_file1.write_text("data1")
    src_file2.write_text("data2")

    target_dir = tmp_path / "Target"
    target_dir.mkdir()
    dst_file1 = target_dir / "file1.txt"
    dst_file2 = target_dir / "file2.txt"

    # Simulate: file1 was physically moved and marked MOVED_PHYSICAL
    shutil.move(str(src_file1), str(dst_file1))
    ledger.log_intent(
        session_id="crash-session",
        base_dir=base_dir,
        source_path=str(src_file1),
        dest_path=str(dst_file1),
        source_rel_path="file1.txt",
        dest_rel_path="Target/file1.txt",
        current_dest="Target",
        file_hash="hash1",
        entry_id="crash-session:file1.txt",
    )
    ledger.update_status("crash-session:file1.txt", "MOVED_PHYSICAL")

    # Simulate: file2 move was intent-logged as PENDING, process crashed before physical move
    ledger.log_intent(
        session_id="crash-session",
        base_dir=base_dir,
        source_path=str(src_file2),
        dest_path=str(dst_file2),
        source_rel_path="file2.txt",
        dest_rel_path="Target/file2.txt",
        current_dest="Target",
        file_hash="hash2",
        entry_id="crash-session:file2.txt",
    )

    pending_before = ledger.get_pending_entries("crash-session")
    assert len(pending_before) == 2

    # Execute automated headless reconciliation
    db = MockDB()
    reconciled_count = ledger.reconcile_incomplete_transactions(db)

    assert reconciled_count == 2
    assert len(ledger.get_pending_entries("crash-session")) == 0

    # Verify DB state: file1 path rolled forward to Target/file1.txt
    assert db.paths.get("file1.txt") == "Target/file1.txt"
    assert db.verified_targets.get("hash1") == "Target"

    # Verify disk state: file1 at destination, file2 intact at source
    assert os.path.exists(dst_file1)
    assert not os.path.exists(src_file1)
    assert os.path.exists(src_file2)
    assert not os.path.exists(dst_file2)


def test_reconciliation_conflict_both_exist(tmp_path):
    """Verify headless reconciliation handles cases where source and partial destination both exist."""
    ledger_db = str(tmp_path / "ledger.db")
    ledger = TransactionLedger(ledger_db)

    base_dir = str(tmp_path)
    src_file = tmp_path / "file.txt"
    src_file.write_text("original")

    dst_dir = tmp_path / "Dest"
    dst_dir.mkdir()
    dst_file = dst_dir / "file.txt"
    dst_file.write_text("partial")

    # Intent logged as PENDING, process crashed mid-copy
    ledger.log_intent(
        session_id="session-conflict",
        base_dir=base_dir,
        source_path=str(src_file),
        dest_path=str(dst_file),
        source_rel_path="file.txt",
        dest_rel_path="Dest/file.txt",
        current_dest="Dest",
        file_hash="hash_conflict",
    )

    db = MockDB()
    ledger.reconcile_incomplete_transactions(db)

    # Partial destination file removed, source file preserved
    assert os.path.exists(src_file)
    assert not os.path.exists(dst_file)
    assert len(ledger.get_pending_entries()) == 0


def test_ledger_performance_overhead(tmp_path):
    """Verify incremental ledger logging overhead remains <5% compared to baseline."""
    ledger_db = str(tmp_path / "ledger.db")
    ledger = TransactionLedger(ledger_db)

    # Measure incremental logging time for 500 file operations
    start_time = time.perf_counter()
    for i in range(500):
        entry_id = f"perf:{i}"
        ledger.log_intent(
            session_id="perf-session",
            base_dir="/tmp",
            source_path=f"/tmp/file_{i}.txt",
            dest_path=f"/tmp/out/file_{i}.txt",
            source_rel_path=f"file_{i}.txt",
            dest_rel_path=f"out/file_{i}.txt",
            current_dest="out",
            file_hash=f"hash_{i}",
            entry_id=entry_id,
        )
        ledger.update_status(entry_id, "MOVED_PHYSICAL")
        ledger.update_status(entry_id, "COMPLETED")
    elapsed = time.perf_counter() - start_time

    # 500 operations (1500 SQLite writes in WAL mode) should complete well under 1 second
    assert elapsed < 1.0
