"""Tests for two-phase off-lock document vectorization."""

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

from app.core.db import Database
from app.core.db_worker import DBWorker


def test_off_lock_term_frequency_precomputation(tmp_path: Path):
    """Verify that text tokenization happens before acquiring the database write lock."""
    db_path = tmp_path / "test_off_lock.db"
    worker = DBWorker()
    db = Database(db_path, worker)

    base_dir = str(tmp_path)

    # Instrument compute_term_frequencies to track thread execution
    original_compute = db.compute_term_frequencies
    call_threads = []

    def spy_compute(text, stop_words_list=None):
        call_threads.append(threading.current_thread())
        return original_compute(text, stop_words_list)

    db.compute_term_frequencies = spy_compute

    # Set up a target path so TF-IDF indexing gets triggered
    doc1 = (base_dir, "doc1.txt", "hash1", "apple banana cherry apple")
    doc2 = (base_dir, "doc2.txt", "hash2", "banana cherry date date")

    # Pre-set target path in DB
    db.upsert_documents(
        [
            (base_dir, "doc1.txt", "hash1", "placeholder"),
            (base_dir, "doc2.txt", "hash2", "placeholder"),
        ]
    )
    db.set_user_verified_target_path(base_dir, "doc1.txt", "folderA")
    db.set_user_verified_target_path(base_dir, "doc2.txt", "folderA")

    call_threads.clear()

    # Now upsert documents with full text
    db.upsert_documents([doc1, doc2])

    # Ensure compute_term_frequencies was called
    assert len(call_threads) >= 2
    # Ensure compute_term_frequencies ran on caller thread or thread pool worker, NOT worker._thread
    for thread in call_threads:
        assert thread != worker.thread, (
            "Tokenization must occur off the DB worker thread"
        )

    # Verify TF-IDF terms in DB match expected pre-computed counts
    stats_n, top_terms, doc_terms, doc_meta = db.get_tfidf_stats(base_dir)
    assert stats_n == 2
    df_dict = {term: df for term, df in top_terms}
    assert df_dict.get("apple") == 1  # in 1 doc
    assert df_dict.get("banana") == 2  # in 2 docs

    tf_dict = {(fp, term): tf for fp, term, tf in doc_terms}
    assert tf_dict.get(("doc1.txt", "apple")) == 2
    assert tf_dict.get(("doc2.txt", "date")) == 2

    worker.stop()


def test_ineligible_format_and_status_string_filtering(tmp_path: Path):
    """Verify that unsupported extensions, audio files, and status strings are filtered out prior to worker execution."""
    db_path = tmp_path / "test_eligibility.db"
    worker = DBWorker()
    db = Database(db_path, worker)

    base_dir = str(tmp_path)

    # Set target paths
    for fp in ["audio.mp3", "image.png", "status.txt", "valid.txt"]:
        db.upsert_documents([(base_dir, fp, f"hash_{fp}", "placeholder")])
        db.set_user_verified_target_path(base_dir, fp, "folderA")

    compute_mock = MagicMock(wraps=db.compute_term_frequencies)
    db.compute_term_frequencies = compute_mock

    documents = [
        (base_dir, "audio.mp3", "h1", "audio transcript terms confidential"),
        (base_dir, "image.png", "h2", "ocr text from image"),
        (base_dir, "status.txt", "h3", "[STATUS:BYPASSED] file was bypassed"),
        (base_dir, "valid.txt", "h4", "valid research document content text"),
    ]

    db.upsert_documents(documents)

    # Only valid.txt should trigger compute_term_frequencies
    assert compute_mock.call_count == 1
    # Verify the argument passed to compute_term_frequencies was for valid.txt
    assert "valid research document content text" in compute_mock.call_args[0][0]

    # Check DB index: terms from audio.mp3, image.png, and status.txt must NOT exist in tfidf_doc_terms
    N, top_terms, doc_terms, meta = db.get_tfidf_stats(base_dir)
    indexed_files = {row[0] for row in doc_terms}
    assert "valid.txt" in indexed_files
    assert "audio.mp3" not in indexed_files
    assert "image.png" not in indexed_files
    assert "status.txt" not in indexed_files

    worker.stop()


def test_graceful_error_handling_invalid_text(tmp_path: Path):
    """Verify that unextractable or invalid text does not crash or stall subsequent queued DB tasks."""
    db_path = tmp_path / "test_error_handling.db"
    worker = DBWorker()
    db = Database(db_path, worker)

    base_dir = str(tmp_path)

    db.upsert_documents([(base_dir, "corrupt.txt", "h1", "old")])
    db.set_user_verified_target_path(base_dir, "corrupt.txt", "folderA")
    db.upsert_documents([(base_dir, "good.txt", "h2", "old")])
    db.set_user_verified_target_path(base_dir, "good.txt", "folderA")

    # Patch compute_term_frequencies to throw exception for corrupt.txt
    orig_compute = db.compute_term_frequencies

    def faulty_compute(text, stop_words_list=None):
        if "CORRUPT" in (text or ""):
            raise ValueError("Corrupt text extraction payload")
        return orig_compute(text, stop_words_list)

    db.compute_term_frequencies = faulty_compute

    docs = [
        (base_dir, "corrupt.txt", "h1", "CORRUPT text data"),
        (base_dir, "good.txt", "h2", "healthy document text data"),
    ]

    # Should complete without throwing exception
    db.upsert_documents(docs)

    # Verify good.txt was indexed cleanly
    N, top_terms, doc_terms, meta = db.get_tfidf_stats(base_dir)
    indexed_files = {row[0] for row in doc_terms}
    assert "good.txt" in indexed_files

    worker.stop()


def test_numerical_parity_and_vocabulary_integrity(tmp_path: Path):
    """Verify vocabulary frequencies match calculated document term totals after batch updates."""
    db_path = tmp_path / "test_parity.db"
    worker = DBWorker()
    db = Database(db_path, worker)

    base_dir = str(tmp_path)

    # Process batch 1
    docs1 = [
        (base_dir, f"doc_{i}.txt", f"hash_{i}", f"common_term unique_{i} keyword")
        for i in range(10)
    ]
    for d in docs1:
        db.upsert_documents([d])
        db.set_user_verified_target_path(base_dir, d[1], "target_folder")

    db.upsert_documents(docs1)

    N, top_terms, doc_terms, meta = db.get_tfidf_stats(base_dir)
    term_dict = {term: total_tf for term, total_tf in top_terms}

    # "common_term" should appear in all 10 documents
    assert term_dict.get("common_term") == 10
    # "keyword" should appear in all 10 documents
    assert term_dict.get("keyword") == 10
    # "unique_0" should appear once
    assert term_dict.get("unique_0") == 1

    worker.stop()


def test_concurrent_read_write_responsiveness(tmp_path: Path):
    """Verify concurrent reads and writes complete cleanly without timing out or blocking UI."""
    db_path = tmp_path / "test_concurrency.db"
    worker = DBWorker()
    db = Database(db_path, worker)

    base_dir = str(tmp_path)

    # Seed initial documents
    for i in range(20):
        db.upsert_document(
            base_dir, f"file_{i}.txt", f"hash_{i}", f"content sample {i}"
        )
        db.set_user_verified_target_path(base_dir, f"file_{i}.txt", "folder1")

    stop_flag = threading.Event()
    read_errors = []
    read_count = [0]

    def concurrent_reader():
        while not stop_flag.is_set():
            try:
                doc = db.get_document(base_dir, "file_0.txt")
                stats = db.get_tfidf_stats(base_dir)
                read_count[0] += 1
                time.sleep(0.001)
            except Exception as e:
                read_errors.append(e)

    reader_thread = threading.Thread(target=concurrent_reader, daemon=True)
    reader_thread.start()

    # Perform active document updates in main thread
    for batch_id in range(5):
        batch_docs = [
            (
                base_dir,
                f"file_{i}.txt",
                f"hash_{i}_{batch_id}",
                f"updated text content iteration {batch_id} word_{i}",
            )
            for i in range(20)
        ]
        db.upsert_documents(batch_docs)

    stop_flag.set()
    reader_thread.join(timeout=2.0)

    assert len(read_errors) == 0, (
        f"Read errors occurred during concurrent write: {read_errors}"
    )
    assert read_count[0] > 0, "Concurrent reads should execute during indexing"

    worker.stop()
