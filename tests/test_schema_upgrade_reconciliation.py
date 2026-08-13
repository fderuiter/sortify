from contextlib import closing

from app.core.db import Database
from app.core.db_conn import clear_connection_cache, get_db_connection
from app.core.db_worker import DBWorker


def test_foreign_keys_enabled_by_default(tmp_path):
    """Verify that every database connection enforces foreign key constraints."""
    db_path = tmp_path / "test_fk.db"
    conn = get_db_connection(db_path)
    with closing(conn.cursor()) as cursor:
        cursor.execute("PRAGMA foreign_keys")
        fk_status = cursor.fetchone()[0]
        assert fk_status == 1


def test_startup_migration_purges_orphans_and_vacuums(tmp_path, mocker):
    """Verify that startup migration from v5 identifies and purges historically orphaned vectors and runs VACUUM."""
    db_path = tmp_path / "test_v5_migration.db"

    # Create a legacy v5 database structure with a document and a vector
    conn = get_db_connection(db_path)
    with conn:
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
        conn.execute("""
            CREATE TABLE document_vectors (
                base_dir TEXT,
                filepath TEXT,
                vector TEXT,
                model_signature TEXT,
                PRIMARY KEY (base_dir, filepath),
                FOREIGN KEY (base_dir, filepath) REFERENCES documents(base_dir, filepath) ON DELETE CASCADE
            )
        """)

        # Insert a valid document and its vector
        conn.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?)",
            ("dir1", "file1.txt", "hash1", "text1", "target1", "good"),
        )
        conn.execute(
            "INSERT INTO document_vectors VALUES (?, ?, ?, ?)",
            ("dir1", "file1.txt", "vector1", "sig1"),
        )

    # Disable foreign keys outside a transaction block so we can insert an orphaned vector
    conn.execute("PRAGMA foreign_keys = OFF")
    with conn:
        conn.execute(
            "INSERT INTO document_vectors VALUES (?, ?, ?, ?)",
            ("dir1", "orphaned.txt", "vector2", "sig1"),
        )

    # Double check that we have 1 document and 2 vectors (one valid, one orphan)
    with conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM documents")
        assert cursor.fetchone()[0] == 1
        cursor.execute("SELECT COUNT(*) FROM document_vectors")
        assert cursor.fetchone()[0] == 2

    clear_connection_cache()

    # Track if VACUUM was called by wrapping get_db_connection
    original_get_db_conn = get_db_connection
    vacuum_called = []

    def mock_get_db_conn(path):
        real_conn = original_get_db_conn(path)

        class ConnProxy:
            def __init__(self, obj):
                self._obj = obj

            def __getattr__(self, name):
                return getattr(self._obj, name)

            def execute(self, sql, *args, **kwargs):
                if isinstance(sql, str) and sql.strip().upper() == "VACUUM":
                    vacuum_called.append(True)
                return self._obj.execute(sql, *args, **kwargs)

            def __enter__(self):
                self._obj.__enter__()
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return self._obj.__exit__(exc_type, exc_val, exc_tb)

        return ConnProxy(real_conn)

    mocker.patch("app.core.db.get_db_connection", mock_get_db_conn)

    # Initialize Database, which should trigger v6 migration, orphaned vector purge, and compaction
    db_worker = DBWorker()
    db = Database(db_path=str(db_path), worker=db_worker)
    db_worker.stop()

    # Verify that VACUUM was indeed executed exactly once during migration
    assert len(vacuum_called) == 1

    # Verify that the orphaned vector was deleted, but the valid one remains.
    with closing(original_get_db_conn(db_path)) as conn, conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA user_version")
        assert cursor.fetchone()[0] == 6

        cursor.execute("SELECT filepath FROM document_vectors")
        vectors = [row[0] for row in cursor.fetchall()]
        assert "file1.txt" in vectors
        assert "orphaned.txt" not in vectors
        assert len(vectors) == 1


def test_auto_cascade_delete_purges_vectors_immediately(tmp_path):
    """Verify that deleting any active document automatically and immediately purges its associated vector representation."""
    db_path = tmp_path / "test_cascade.db"
    db_worker = DBWorker()
    db = Database(db_path=str(db_path), worker=db_worker)

    try:
        # Insert a document and its vector
        db.upsert_document("dir1", "file1.txt", "hash1", "extracted_text")
        db.upsert_document_vectors("dir1", [("file1.txt", [0.1, 0.2, 0.3])], "sig1")

        # Verify vector exists
        vec = db.get_document_vector("dir1", "file1.txt")
        assert vec == [0.1, 0.2, 0.3]

        # Now remove the document
        db.remove_document("dir1", "file1.txt")

        # Verify vector is automatically and immediately deleted (purged)
        vec_after = db.get_document_vector("dir1", "file1.txt")
        assert vec_after is None
    finally:
        db_worker.stop()
