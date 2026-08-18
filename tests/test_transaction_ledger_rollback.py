import os
from contextlib import closing

import pytest

from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_conn import clear_connection_cache, get_db_connection
from app.core.db_worker import DBWorker
from app.core.extractor import get_file_hash
from app.core.history import HistoryManager
from app.core.mover import execute_moves


@pytest.fixture
def transaction_env(tmp_path):
    base_dir = tmp_path / "test_base"
    base_dir.mkdir()
    db_path = tmp_path / "docs.db"
    cache_path = tmp_path / "cache.db"
    history_db_path = tmp_path / "history.db"

    clear_connection_cache()
    db_worker = DBWorker()
    db = Database(db_path=str(db_path), worker=db_worker)
    db.init_db()

    cache_mgr = CacheManager(str(cache_path), worker=db_worker)
    history_manager = HistoryManager(db=db, cache_manager=cache_mgr, db_path=str(history_db_path))

    yield str(base_dir), db, history_manager, db_worker

    db_worker.stop()
    clear_connection_cache()


def test_schema_migration_v6(tmp_path):
    """Test that transaction_ledger table and index are created via standard schema migration."""
    db_path = tmp_path / "test_v5.db"

    # Create v5 database
    with closing(get_db_connection(db_path)) as conn, conn:
        conn.execute("PRAGMA user_version = 5")
        conn.execute("""
            CREATE TABLE documents (
                base_dir TEXT,
                filepath TEXT,
                file_hash TEXT,
                extracted_text TEXT,
                user_verified_target_path TEXT,
                rating TEXT,
                PRIMARY KEY (base_dir, filepath)
            )
        """)

    clear_connection_cache()
    db_worker = DBWorker()
    db = Database(db_path=str(db_path), worker=db_worker)
    db.init_db()
    db_worker.stop()

    with closing(get_db_connection(db_path)) as conn, conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA user_version")
        assert cursor.fetchone()[0] == 6

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='transaction_ledger'")
        assert cursor.fetchone() is not None

        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_transaction_ledger_session'")
        assert cursor.fetchone() is not None


def test_transaction_ledger_batch_logging(transaction_env):
    """Test that batch moves log atomic relocation steps sequentially into transaction_ledger."""
    base_dir, db, history_manager, db_worker = transaction_env

    # Create sample files
    f1 = os.path.join(base_dir, "doc1.txt")
    f2 = os.path.join(base_dir, "doc2.pdf")
    with open(f1, "w") as f:
        f.write("content 1")
    with open(f2, "w") as f:
        f.write("content 2")

    hash1 = get_file_hash(f1)
    hash2 = get_file_hash(f2)

    db.upsert_document(base_dir, "doc1.txt", hash1, "content 1")
    db.upsert_document(base_dir, "doc2.pdf", hash2, "content 2")

    plan = {
        "Docs": {
            "doc1.txt": {
                "__type__": "file",
                "relative_source": "../doc1.txt",
                "target_filename": "doc1.txt",
            },
            "doc2.pdf": {
                "__type__": "file",
                "relative_source": "../doc2.pdf",
                "target_filename": "doc2.pdf",
            },
        }
    }

    execute_moves(base_dir, plan, db, history_manager)

    sessions = history_manager.get_sessions()
    assert len(sessions) > 0
    session_id = sessions[0]["session_id"]

    steps = db.get_transaction_steps(session_id)
    assert len(steps) == 2

    # Check sequence
    assert steps[0]["step_number"] == 1
    assert steps[1]["step_number"] == 2

    # Check fields
    paths = {s["source_path"]: s["destination_path"] for s in steps}
    assert paths.get("doc1.txt") == "Docs/doc1.txt"
    assert paths.get("doc2.pdf") == "Docs/doc2.pdf"

    hashes = {s["source_path"]: s["file_hash"] for s in steps}
    assert hashes.get("doc1.txt") == hash1
    assert hashes.get("doc2.pdf") == hash2


def test_reverse_step_rollback_with_safety_snapshot(transaction_env):
    """Test that rollback replays transaction step entries in reverse order and generates a safety snapshot."""
    base_dir, db, history_manager, db_worker = transaction_env

    f1 = os.path.join(base_dir, "fileA.txt")
    f2 = os.path.join(base_dir, "fileB.txt")
    with open(f1, "w") as f:
        f.write("A data")
    with open(f2, "w") as f:
        f.write("B data")

    hashA = get_file_hash(f1)
    hashB = get_file_hash(f2)

    db.upsert_document(base_dir, "fileA.txt", hashA, "A data")
    db.upsert_document(base_dir, "fileB.txt", hashB, "B data")

    plan = {
        "Archive": {
            "fileA.txt": {
                "__type__": "file",
                "relative_source": "../fileA.txt",
                "target_filename": "fileA.txt",
            },
            "fileB.txt": {
                "__type__": "file",
                "relative_source": "../fileB.txt",
                "target_filename": "fileB.txt",
            },
        }
    }

    execute_moves(base_dir, plan, db, history_manager)

    sessions = history_manager.get_sessions()
    session_id = sessions[0]["session_id"]

    # Verify moved
    assert os.path.exists(os.path.join(base_dir, "Archive", "fileA.txt"))
    assert os.path.exists(os.path.join(base_dir, "Archive", "fileB.txt"))
    assert not os.path.exists(f1)
    assert not os.path.exists(f2)

    # Perform rollback
    history_manager.rollback(session_id)

    # Verify restored to original paths
    assert os.path.exists(f1)
    assert os.path.exists(f2)
    with open(f1) as f:
        assert f.read() == "A data"
    with open(f2) as f:
        assert f.read() == "B data"

    # Verify DB documents reverted
    docA = db.get_document(base_dir, "fileA.txt")
    docB = db.get_document(base_dir, "fileB.txt")
    assert docA is not None
    assert docB is not None

    # Verify safety snapshot session was created
    updated_sessions = history_manager.get_sessions()
    assert len(updated_sessions) >= 2


def test_hash_mismatch_halts_rollback_and_restores_safety_state(transaction_env):
    """Test that mismatched file content hash during rollback halts step replay and restores safety state."""
    base_dir, db, history_manager, db_worker = transaction_env

    f1 = os.path.join(base_dir, "original.txt")
    with open(f1, "w") as f:
        f.write("original content")

    hash1 = get_file_hash(f1)
    db.upsert_document(base_dir, "original.txt", hash1, "original content")

    plan = {
        "Sorted": {
            "original.txt": {
                "__type__": "file",
                "relative_source": "../original.txt",
                "target_filename": "original.txt",
            }
        }
    }

    execute_moves(base_dir, plan, db, history_manager)

    sessions = history_manager.get_sessions()
    session_id = sessions[0]["session_id"]

    moved_file = os.path.join(base_dir, "Sorted", "original.txt")
    assert os.path.exists(moved_file)

    # Corrupt the moved file content so hash will mismatch
    with open(moved_file, "w") as f:
        f.write("TAMPERED DATA")

    # Rollback should detect hash mismatch, halt, restore state from safety snapshot, and raise ValueError
    with pytest.raises(ValueError, match="Rollback halted"):
        history_manager.rollback(session_id)

    # State restored from safety snapshot: the file at "Sorted/original.txt" should remain/be restored as tampered or safety state
    assert os.path.exists(moved_file)
