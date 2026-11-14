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


def test_startup_purging_zero_decryption_calls_for_valid_documents(
    tmp_path, monkeypatch
):
    """Verify that legacy audio records are purged on startup while zero decryption calls occur for valid documents."""
    import app.core.path_utils as path_utils

    db_path = tmp_path / "zero_decrypt_test.db"
    db_worker = DBWorker()
    db = Database(db_path=db_path, worker=db_worker)

    base_dir = str(tmp_path)
    audio_path_lower = "audio_memo.wav"
    audio_path_upper = "recordings/OLD_MEMO.MP3"
    audio_path_win = "win\\legacy_voice.FLAC"
    valid_pdf = "documents/contract.pdf"
    valid_docx = "reports\\Q3_REPORT.DOCX"
    unsupported_file = "assets/photo.PNG"

    db.upsert_documents(
        [
            (base_dir, audio_path_lower, "h1", "audio terms content"),
            (base_dir, audio_path_upper, "h2", "audio terms upper content"),
            (base_dir, audio_path_win, "h3", "audio terms flac content"),
            (base_dir, valid_pdf, "h4", "contract terms content"),
            (base_dir, valid_docx, "h5", "report terms content"),
            (base_dir, unsupported_file, "h6", "photo binary content"),
        ]
    )

    # Manually populate tfidf_doc_terms and tfidf_vocab simulating legacy database state
    with closing(get_db_connection(str(db_path))) as conn, conn:
        conn.executemany(
            "INSERT INTO tfidf_doc_terms (base_dir, filepath, term, tf) VALUES (?, ?, ?, ?)",
            [
                (base_dir, audio_path_lower, "audio", 1),
                (base_dir, audio_path_upper, "audio", 1),
                (base_dir, audio_path_win, "audio", 1),
                (base_dir, unsupported_file, "photo", 1),
                (base_dir, valid_pdf, "contract", 1),
                (base_dir, valid_docx, "report", 1),
            ],
        )
        conn.executemany(
            "INSERT INTO tfidf_vocab (base_dir, term, df) VALUES (?, ?, ?)",
            [
                (base_dir, "audio", 3),
                (base_dir, "photo", 1),
                (base_dir, "contract", 1),
                (base_dir, "report", 1),
            ],
        )

    db_worker.stop()
    clear_connection_cache(only_current_and_inactive=False)

    # Intercept resolve_db_crypto to monitor decryption calls on startup
    decrypt_calls = []
    orig_resolve = path_utils.resolve_db_crypto

    def counting_resolve(path):
        crypto = orig_resolve(path)
        orig_decrypt = crypto.decrypt_text

        def spy_decrypt(data):
            decrypt_calls.append(data)
            return orig_decrypt(data)

        crypto.decrypt_text = spy_decrypt
        return crypto

    monkeypatch.setattr(path_utils, "resolve_db_crypto", counting_resolve)

    # Startup / re-initialize Database
    new_worker = DBWorker()
    new_db = Database(db_path=db_path, worker=new_worker)
    new_worker.stop()

    # CRITICAL: Zero decryption calls MUST occur for valid documents and purged records on startup
    assert len(decrypt_calls) == 0, (
        f"Expected 0 decryption calls, got {len(decrypt_calls)}"
    )

    # Verify purge results
    with closing(get_db_connection(str(db_path))) as conn, conn:
        cursor = conn.cursor()
        # Audio and unsupported records must be purged
        for purged_path in [
            audio_path_lower,
            audio_path_upper,
            audio_path_win,
            unsupported_file,
        ]:
            cursor.execute(
                "SELECT COUNT(*) FROM tfidf_doc_terms WHERE base_dir = ? AND filepath = ?",
                (base_dir, purged_path.replace("\\", "/")),
            )
            assert cursor.fetchone()[0] == 0, f"Expected {purged_path} to be purged"

        # Valid document records must remain
        for kept_path in [valid_pdf, valid_docx]:
            cursor.execute(
                "SELECT COUNT(*) FROM tfidf_doc_terms WHERE base_dir = ? AND (filepath = ? OR filepath = ?)",
                (base_dir, kept_path, kept_path.replace("\\", "/")),
            )
            assert cursor.fetchone()[0] == 1, f"Expected {kept_path} terms to remain"

        # Purged vocab terms must be deleted
        cursor.execute(
            "SELECT COUNT(*) FROM tfidf_vocab WHERE base_dir = ? AND term IN ('audio', 'photo')",
            (base_dir,),
        )
        assert cursor.fetchone()[0] == 0

        # Valid vocab terms must remain
        cursor.execute(
            "SELECT term, df FROM tfidf_vocab WHERE base_dir = ? ORDER BY term",
            (base_dir,),
        )
        remaining_vocab = dict(cursor.fetchall())
        assert remaining_vocab == {"contract": 1, "report": 1}


def test_startup_no_legacy_audio_zero_documents_table_queries(tmp_path):
    """Verify that when no legacy audio documents exist, startup purge runs zero queries to documents table."""
    db_path = tmp_path / "no_legacy_test.db"
    db_worker = DBWorker()
    db = Database(db_path=db_path, worker=db_worker)

    base_dir = str(tmp_path)
    valid_doc1 = "doc1.pdf"
    valid_doc2 = "sub\\doc2.txt"

    db.upsert_documents(
        [
            (base_dir, valid_doc1, "h1", "sample content 1"),
            (base_dir, valid_doc2, "h2", "sample content 2"),
        ]
    )

    with closing(get_db_connection(str(db_path))) as conn, conn:
        conn.executemany(
            "INSERT INTO tfidf_doc_terms (base_dir, filepath, term, tf) VALUES (?, ?, ?, ?)",
            [
                (base_dir, valid_doc1, "sample", 1),
                (base_dir, valid_doc2.replace("\\", "/"), "content", 1),
            ],
        )
        conn.executemany(
            "INSERT INTO tfidf_vocab (base_dir, term, df) VALUES (?, ?, ?)",
            [
                (base_dir, "sample", 1),
                (base_dir, "content", 1),
            ],
        )

    db_worker.stop()
    clear_connection_cache(only_current_and_inactive=False)

    # Monkeypatch execute on Database connection to track any query referencing documents table
    import app.core.db as db_module

    executed_queries = []
    orig_get_db_connection = db_module.get_db_connection

    def tracking_get_db_connection(*args, **kwargs):
        conn = orig_get_db_connection(*args, **kwargs)
        conn.set_trace_callback(executed_queries.append)
        return conn

    db_module.get_db_connection = tracking_get_db_connection
    try:
        new_worker = DBWorker()
        new_db = Database(db_path=db_path, worker=new_worker)
        new_worker.stop()
    finally:
        db_module.get_db_connection = orig_get_db_connection

    # Filter out schema migration / vector purge queries, specifically checking for queries
    # reading extracted_text from documents during startup purge
    extracted_text_queries = [
        q
        for q in executed_queries
        if "extracted_text" in str(q).lower() and "documents" in str(q).lower()
    ]
    assert len(extracted_text_queries) == 0, (
        f"Expected 0 queries reading extracted_text from documents, got: {extracted_text_queries}"
    )


def test_is_tfidf_eligible_extension_normalization():
    """Verify extension validation correctly handles casing, backslashes, and unsupported types."""
    # Audio extensions -> False
    assert Database._is_tfidf_eligible_extension("test.mp3") is False
    assert Database._is_tfidf_eligible_extension("folder/TEST.WAV") is False
    assert Database._is_tfidf_eligible_extension("windows\\path\\MEMO.M4A") is False
    assert Database._is_tfidf_eligible_extension("deep\\nested\\file.flac") is False

    # Unsupported non-audio extensions -> False
    assert Database._is_tfidf_eligible_extension("image.png") is False
    assert Database._is_tfidf_eligible_extension("archive.ZIP") is False
    assert Database._is_tfidf_eligible_extension("binary.exe") is False
    assert Database._is_tfidf_eligible_extension("") is False
    assert Database._is_tfidf_eligible_extension(None) is False

    # Supported document extensions -> True
    assert Database._is_tfidf_eligible_extension("document.pdf") is True
    assert Database._is_tfidf_eligible_extension("REPORT.PDF") is True
    assert Database._is_tfidf_eligible_extension("folder/data.csv") is True
    assert Database._is_tfidf_eligible_extension("windows\\path\\sheet.XLSX") is True
    assert Database._is_tfidf_eligible_extension("sub\\notes.TXT") is True
    assert Database._is_tfidf_eligible_extension("file.docx") is True
    assert Database._is_tfidf_eligible_extension("sheet.xls") is True
