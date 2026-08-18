import sqlite3 as std_sqlite3
from contextlib import closing

from app.core.db import Database
from app.core.db_conn import clear_connection_cache, get_db_connection
from app.core.db_worker import DBWorker


def test_audio_file_exclusion_from_tfidf_indexing(tmp_path):
    """Verify audio files are excluded from TF-IDF index tables while non-audio files are indexed."""
    db_path = tmp_path / "test_audio_exclusion.db"
    db_worker = DBWorker()
    db = Database(db_path=db_path, worker=db_worker)

    base_dir = str(tmp_path)
    audio_path = "recordings/confidential_voice_memo.mp3"
    audio_text = (
        "secret clinical diagnosis patient conversation confidential audio terms"
    )
    audio_hash = "hash_audio_123"

    text_path = "documents/clinical_notes.txt"
    text_text = "clinical diagnosis report notes"
    text_hash = "hash_text_456"

    # Upsert both audio and text documents
    db.upsert_documents(
        [
            (base_dir, audio_path, audio_hash, audio_text),
            (base_dir, text_path, text_hash, text_text),
        ]
    )

    # Assign target directories (triggers TF-IDF index generation for eligible documents)
    db.set_user_verified_target_path(base_dir, audio_path, "FolderA")
    db.set_user_verified_target_path(base_dir, text_path, "FolderB")

    # 1. Verify full document text persistence and retrieval for audio
    retrieved_audio = db.get_document(base_dir, audio_path)
    assert retrieved_audio is not None
    assert retrieved_audio["extracted_text"] == audio_text

    # 2. Verify full document text persistence and retrieval for text
    retrieved_text = db.get_document(base_dir, text_path)
    assert retrieved_text is not None
    assert retrieved_text["extracted_text"] == text_text

    # 3. Verify indexing tables: Audio terms must NOT be present in tfidf_doc_terms
    with closing(get_db_connection(str(db_path))) as conn, conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM tfidf_doc_terms WHERE base_dir = ? AND filepath = ?",
            (base_dir, audio_path),
        )
        assert cursor.fetchone()[0] == 0

        # Text document terms MUST be present
        cursor.execute(
            "SELECT COUNT(*) FROM tfidf_doc_terms WHERE base_dir = ? AND filepath = ?",
            (base_dir, text_path),
        )
        assert cursor.fetchone()[0] > 0

        # Terms unique to audio document (e.g., 'confidential') must NOT be in tfidf_vocab
        cursor.execute(
            "SELECT COUNT(*) FROM tfidf_vocab WHERE base_dir = ? AND term = ?",
            (base_dir, "confidential"),
        )
        assert cursor.fetchone()[0] == 0

    db_worker.stop()


def test_sqlcipher_encryption_on_disk(tmp_path):
    """Verify database on disk is encrypted with SQLCipher and unencrypted connections are rejected."""
    db_path = tmp_path / "test_encrypted.db"
    db_worker = DBWorker()
    db = Database(db_path=db_path, worker=db_worker)

    base_dir = str(tmp_path)
    db.upsert_document(base_dir, "sample.txt", "hash1", "sensitive data content")
    db_worker.stop()
    clear_connection_cache(only_current_and_inactive=False)

    # Standard SQLite without encryption key MUST fail to open/query the database
    with closing(std_sqlite3.connect(str(db_path))) as std_conn:
        std_cursor = std_conn.cursor()
        try:
            std_cursor.execute("SELECT count(*) FROM documents")
            # If standard sqlite can read it, it's not encrypted!
            assert False, "Unencrypted connection succeeded on encrypted database file"
        except std_sqlite3.Error as e:
            assert "not a database" in str(e).lower() or "encrypted" in str(e).lower()

    # Connection using get_db_connection (with valid SQLCipher key) MUST succeed
    with closing(get_db_connection(str(db_path))) as cipher_conn, cipher_conn:
        cursor = cipher_conn.cursor()
        cursor.execute("SELECT count(*) FROM documents")
        assert cursor.fetchone()[0] == 1


def test_startup_purging_legacy_audio_terms(tmp_path):
    """Verify application startup automatically purges legacy audio terms from search tables."""
    db_path = tmp_path / "legacy_test.db"
    db_worker = DBWorker()
    db = Database(db_path=db_path, worker=db_worker)

    base_dir = str(tmp_path)
    audio_path = "voice_memo.wav"
    text_path = "standard_doc.pdf"

    # Insert documents
    db.upsert_documents(
        [
            (base_dir, audio_path, "hash_audio", "audio transcript terms legacy"),
            (base_dir, text_path, "hash_pdf", "pdf document terms legacy"),
        ]
    )

    # Manually simulate legacy database state where audio terms were previously indexed
    with closing(get_db_connection(str(db_path))) as conn, conn:
        conn.execute(
            "INSERT INTO tfidf_doc_terms (base_dir, filepath, term, tf) VALUES (?, ?, ?, ?)",
            (base_dir, audio_path, "transcript", 1),
        )
        conn.execute(
            "INSERT INTO tfidf_doc_terms (base_dir, filepath, term, tf) VALUES (?, ?, ?, ?)",
            (base_dir, audio_path, "legacy", 1),
        )
        conn.execute(
            "INSERT INTO tfidf_doc_terms (base_dir, filepath, term, tf) VALUES (?, ?, ?, ?)",
            (base_dir, text_path, "legacy", 1),
        )
        conn.execute(
            "INSERT INTO tfidf_doc_terms (base_dir, filepath, term, tf) VALUES (?, ?, ?, ?)",
            (base_dir, text_path, "pdf", 1),
        )

        conn.execute(
            "INSERT INTO tfidf_vocab (base_dir, term, df) VALUES (?, ?, ?)",
            (base_dir, "transcript", 1),
        )
        conn.execute(
            "INSERT INTO tfidf_vocab (base_dir, term, df) VALUES (?, ?, ?)",
            (base_dir, "legacy", 2),
        )
        conn.execute(
            "INSERT INTO tfidf_vocab (base_dir, term, df) VALUES (?, ?, ?)",
            (base_dir, "pdf", 1),
        )

    db_worker.stop()
    clear_connection_cache(only_current_and_inactive=False)

    # Re-initialize Database (simulates app launch / startup migration)
    new_worker = DBWorker()
    new_db = Database(db_path=db_path, worker=new_worker)
    new_worker.stop()

    # Verify purge
    with closing(get_db_connection(str(db_path))) as conn, conn:
        cursor = conn.cursor()
        # Audio terms in tfidf_doc_terms MUST be purged
        cursor.execute(
            "SELECT COUNT(*) FROM tfidf_doc_terms WHERE base_dir = ? AND filepath = ?",
            (base_dir, audio_path),
        )
        assert cursor.fetchone()[0] == 0

        # Non-audio document terms in tfidf_doc_terms MUST remain
        cursor.execute(
            "SELECT COUNT(*) FROM tfidf_doc_terms WHERE base_dir = ? AND filepath = ?",
            (base_dir, text_path),
        )
        assert cursor.fetchone()[0] == 2

        # Term unique to audio ('transcript') MUST be deleted from tfidf_vocab
        cursor.execute(
            "SELECT COUNT(*) FROM tfidf_vocab WHERE base_dir = ? AND term = ?",
            (base_dir, "transcript"),
        )
        assert cursor.fetchone()[0] == 0

        # Shared term ('legacy') df MUST be decremented from 2 to 1
        cursor.execute(
            "SELECT df FROM tfidf_vocab WHERE base_dir = ? AND term = ?",
            (base_dir, "legacy"),
        )
        assert cursor.fetchone()[0] == 1


def test_audio_document_path_update_bypasses_tfidf(tmp_path):
    """Verify updating audio file paths or verified targets preserves document text but excludes index terms."""
    db_path = tmp_path / "test_path_update.db"
    db_worker = DBWorker()
    db = Database(db_path=db_path, worker=db_worker)

    base_dir = str(tmp_path)
    old_path = "recordings/audio1.m4a"
    new_path = "archive/audio1.m4a"
    text = "m4a audio memo contents"

    db.upsert_document(base_dir, old_path, "hash_m4a", text)
    db.set_user_verified_target_path(base_dir, old_path, "recordings")

    # Rename audio document
    db.update_document_path(base_dir, old_path, new_path)

    # Verify retrieved text from new path
    doc = db.get_document(base_dir, new_path)
    assert doc is not None
    assert doc["extracted_text"] == text

    # Verify zero terms in tfidf_doc_terms
    with closing(get_db_connection(str(db_path))) as conn, conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM tfidf_doc_terms WHERE base_dir = ?",
            (base_dir,),
        )
        assert cursor.fetchone()[0] == 0

    db_worker.stop()
