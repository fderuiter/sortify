import os
import shutil

import pytest

from app.core.analyzer import pre_fetch_historical_corpus
from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_conn import get_db_connection
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager


@pytest.fixture
def test_snapshot_env(tmp_path):
    base_dir = str(tmp_path / "workspace")
    os.makedirs(base_dir, exist_ok=True)

    db_worker = DBWorker()
    db_path = tmp_path / "autosorter.db"
    db = Database(db_path, worker=db_worker)

    cache_path = tmp_path / "cache.db"
    cache = CacheManager(str(cache_path), worker=db_worker)

    history_db_path = str(tmp_path / "history.db")
    history_manager = HistoryManager(db, cache, history_db_path)

    yield base_dir, db, cache, history_manager, db_worker
    db_worker.stop()


def test_new_snapshot_stores_user_verified_target_paths(test_snapshot_env):
    base_dir, db, cache, history_manager, db_worker = test_snapshot_env

    # 1. Create test file and register in DB with user_verified_target_path
    file_rel = "doc1.txt"
    file_abs = os.path.join(base_dir, file_rel)
    with open(file_abs, "w") as f:
        f.write("Test content for document 1")

    db.upsert_document(base_dir, file_rel, "hash_doc1", "Test content for document 1")
    db.set_user_verified_target_path(base_dir, file_rel, "Financial_Reports")

    # Verify active document record has target path set
    fetched_path = db.get_user_verified_target_path(base_dir, file_rel)
    assert fetched_path == "Financial_Reports"

    # 2. Create snapshot
    session_id = history_manager.create_snapshot(base_dir)

    # 3. Verify snapshot_documents table stores user_verified_target_path
    conn = get_db_connection(history_manager.db_path)
    with conn:
        cur = conn.execute(
            "SELECT filepath, user_verified_target_path FROM snapshot_documents WHERE session_id = ?",
            (session_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 1
    assert rows[0][0] == file_rel
    assert rows[0][1] == "Financial_Reports"


def test_rollback_restores_user_verified_target_paths(test_snapshot_env):
    base_dir, db, cache, history_manager, db_worker = test_snapshot_env

    # 1. Setup initial document state with verified target
    file_rel = "doc1.txt"
    file_abs = os.path.join(base_dir, file_rel)
    with open(file_abs, "w") as f:
        f.write("Test content for document 1")

    db.upsert_document(base_dir, file_rel, "hash_doc1", "Test content for document 1")
    db.set_user_verified_target_path(base_dir, file_rel, "Financial_Reports")

    # Capture snapshot
    snapshot_session_id = history_manager.create_snapshot(base_dir)

    # 2. Simulate user or AI moving document and changing verified target
    target_dir = os.path.join(base_dir, "Wrong_Folder")
    os.makedirs(target_dir, exist_ok=True)
    moved_abs = os.path.join(target_dir, "doc1.txt")
    shutil.move(file_abs, moved_abs)

    moved_rel = os.path.join("Wrong_Folder", "doc1.txt")
    db.update_document_path(base_dir, file_rel, moved_rel)
    db.set_user_verified_target_path(base_dir, moved_rel, "Wrong_Folder")

    # Verify altered state before rollback
    assert db.get_user_verified_target_path(base_dir, moved_rel) == "Wrong_Folder"

    # 3. Execute rollback to original snapshot
    history_manager.rollback(snapshot_session_id)

    # 4. Verify file restored to doc1.txt and user_verified_target_path restored to "Financial_Reports"
    assert os.path.exists(file_abs)
    assert not os.path.exists(moved_abs)

    restored_target = db.get_user_verified_target_path(base_dir, file_rel)
    assert restored_target == "Financial_Reports"


def test_rollback_to_legacy_snapshot_uses_default_fallback(test_snapshot_env):
    base_dir, db, cache, history_manager, db_worker = test_snapshot_env

    file_rel = "legacy_doc.txt"
    file_abs = os.path.join(base_dir, file_rel)
    with open(file_abs, "w") as f:
        f.write("Legacy document content")

    db.upsert_document(base_dir, file_rel, "legacy_hash", "Legacy document content")

    # Create snapshot using history manager
    legacy_session_id = history_manager.create_snapshot(base_dir)

    # Simulate legacy snapshot created on older schema without target path data
    conn = get_db_connection(history_manager.db_path)
    with conn:
        conn.execute(
            "UPDATE snapshot_documents SET user_verified_target_path = NULL WHERE session_id = ?",
            (legacy_session_id,),
        )

    # Change active document target path to something else before rollback
    db.set_user_verified_target_path(base_dir, file_rel, "New_Target")
    assert db.get_user_verified_target_path(base_dir, file_rel) == "New_Target"

    # Roll back to legacy snapshot
    history_manager.rollback(legacy_session_id)

    # Verify rollback completed successfully and target path fell back defensively to None
    restored_target = db.get_user_verified_target_path(base_dir, file_rel)
    assert restored_target is None


def test_rollback_creates_safety_backup_snapshot(test_snapshot_env):
    base_dir, db, cache, history_manager, db_worker = test_snapshot_env

    file_rel = "safety_doc.txt"
    file_abs = os.path.join(base_dir, file_rel)
    with open(file_abs, "w") as f:
        f.write("Safety test content")

    db.upsert_document(base_dir, file_rel, "safety_hash", "Safety test content")
    db.set_user_verified_target_path(base_dir, file_rel, "Folder_A")

    snapshot_session_id = history_manager.create_snapshot(base_dir)

    # Change state
    db.set_user_verified_target_path(base_dir, file_rel, "Folder_B")

    conn = get_db_connection(history_manager.db_path)
    with conn:
        count_before = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    # Perform rollback
    history_manager.rollback(snapshot_session_id)

    # Verify a safety snapshot session was created in history DB
    with conn:
        count_after = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    assert count_after >= count_before + 1


def test_ai_categorization_retrieves_restored_exemplars(test_snapshot_env):
    base_dir, db, cache, history_manager, db_worker = test_snapshot_env

    file_rel = "doc_ai.txt"
    file_abs = os.path.join(base_dir, file_rel)
    with open(file_abs, "w") as f:
        f.write("Invoice for ACME Corp consulting services")

    db.upsert_document(base_dir, file_rel, "hash_ai", "Invoice for ACME Corp consulting services")
    db.set_user_verified_target_path(base_dir, file_rel, "Invoices/2026")

    # Snapshot 1: Verified target is "Invoices/2026"
    snapshot_1 = history_manager.create_snapshot(base_dir)

    # User re-verifies target to "Accounting/Invoices"
    db.set_user_verified_target_path(base_dir, file_rel, "Accounting/Invoices")
    snapshot_2 = history_manager.create_snapshot(base_dir)

    # User accidentally sets target to "Trash"
    db.set_user_verified_target_path(base_dir, file_rel, "Trash")

    # Verify pre_fetch_historical_corpus sees "Trash" before rollback
    corpus_before = pre_fetch_historical_corpus(db, base_dir, filenames=[file_rel], documents=[])
    examples_before = corpus_before.get("examples", [])
    assert len(examples_before) == 1
    assert examples_before[0]["user_verified_target_path"] == "Trash"

    # Roll back to snapshot 1
    history_manager.rollback(snapshot_1)

    # Verify pre_fetch_historical_corpus retrieves restored exemplar "Invoices/2026" immediately
    corpus_after = pre_fetch_historical_corpus(db, base_dir, filenames=[file_rel], documents=[])
    examples_after = corpus_after.get("examples", [])
    assert len(examples_after) == 1
    assert examples_after[0]["user_verified_target_path"] == "Invoices/2026"


def test_snapshot_rollback_with_windows_backslash_paths(test_snapshot_env):
    base_dir, db, cache, history_manager, db_worker = test_snapshot_env

    # 1. Setup file with Windows backslash relative path
    sub_dir = os.path.join(base_dir, "subfolder")
    os.makedirs(sub_dir, exist_ok=True)
    file_abs = os.path.join(sub_dir, "doc1.txt")
    with open(file_abs, "w") as f:
        f.write("Windows path content")

    file_rel_win = "subfolder\\doc1.txt"
    file_rel_norm = "subfolder/doc1.txt"

    db.upsert_document(base_dir, file_rel_win, "hash_win", "Windows path content")
    db.set_user_verified_target_path(base_dir, file_rel_win, "Target_Folder")

    # Verify target path is set regardless of slash convention
    assert db.get_user_verified_target_path(base_dir, file_rel_win) == "Target_Folder"
    assert db.get_user_verified_target_path(base_dir, file_rel_norm) == "Target_Folder"

    # Create snapshot
    snapshot_id = history_manager.create_snapshot(base_dir)

    # Change target path
    db.set_user_verified_target_path(base_dir, file_rel_win, "Changed_Folder")
    assert db.get_user_verified_target_path(base_dir, file_rel_win) == "Changed_Folder"

    # Roll back
    history_manager.rollback(snapshot_id)

    # Verify target path restored
    assert db.get_user_verified_target_path(base_dir, file_rel_win) == "Target_Folder"
    assert db.get_user_verified_target_path(base_dir, file_rel_norm) == "Target_Folder"

