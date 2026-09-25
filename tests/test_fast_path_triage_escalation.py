"""Unit and performance tests for fast-path triage pipeline, text quality confidence, LRU matrix cache, and async escalation queues."""

import os
import time
from pathlib import Path

from PIL import Image

from app.core.cache import SparseMatrixLRUCache
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.extractor import evaluate_text_confidence, fast_triage_extract


def _is_ci_or_parallel() -> bool:
    return (
        "PYTEST_XDIST_WORKER" in os.environ
        or "CI" in os.environ
        or os.environ.get("GITHUB_ACTIONS") == "true"
    )


def test_text_confidence_evaluation():
    """Verify text confidence scoring logic across standard, complex, and unreadable formats."""
    # Standard text and tabular formats
    assert evaluate_text_confidence("Invoice #1024 for medical services", ".txt") == 1.0
    assert evaluate_text_confidence("Header,Value,Date\nA,1,2026-01-01", ".csv") == 1.0
    assert evaluate_text_confidence("# Markdown Title\nSome content.", ".md") == 1.0

    # Image formats return 0.0 (escalate to VLM/OCR)
    assert evaluate_text_confidence("Some OCR Text", ".png") == 0.0
    assert evaluate_text_confidence("Some OCR Text", ".jpg") == 0.0

    # Clean PDF text vs empty/scanned PDF text
    assert evaluate_text_confidence("Medical trial summary report with detailed findings.", ".pdf") >= 0.8
    assert evaluate_text_confidence("", ".pdf") == 0.0
    assert evaluate_text_confidence("a b", ".pdf") < 0.8


def test_standard_text_and_csv_triage_under_50ms(tmp_path: Path):
    """Verify standard text and CSV files complete triage in under 50ms on fast path."""
    text_file = tmp_path / "invoice.txt"
    text_file.write_text("Medical Invoice #8841 for patient treatment", encoding="utf-8")

    csv_file = tmp_path / "report.csv"
    csv_file.write_text("Patient,ID,Status\nJohn Doe,101,Active", encoding="utf-8")

    start_time = time.perf_counter()
    res_text = fast_triage_extract(str(text_file))
    elapsed_text_ms = (time.perf_counter() - start_time) * 1000.0

    start_time = time.perf_counter()
    res_csv = fast_triage_extract(str(csv_file))
    elapsed_csv_ms = (time.perf_counter() - start_time) * 1000.0

    threshold = 200.0 if _is_ci_or_parallel() else 50.0
    assert "Medical Invoice" in res_text
    assert "John Doe" in res_csv
    assert elapsed_text_ms < threshold, f"Text triage expected < {threshold}ms, got {elapsed_text_ms:.2f}ms"
    assert elapsed_csv_ms < threshold, f"CSV triage expected < {threshold}ms, got {elapsed_csv_ms:.2f}ms"


def test_low_confidence_escalation_non_blocking(tmp_path: Path):
    """Verify low-confidence documents escalate to asynchronous background queues without caller blocking."""
    img_file = tmp_path / "scanned_doc.png"
    img = Image.new("RGB", (150, 150), color="white")
    img.save(img_file)

    pdf_file = tmp_path / "blank_scanned.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 empty pdf stub")

    db_worker = DBWorker()
    db_path = tmp_path / "escalation.db"
    db = Database(db_path, db_worker)

    try:
        start_time = time.perf_counter()
        res_img = fast_triage_extract(str(img_file), db=db, base_dir=str(tmp_path))
        elapsed_img_ms = (time.perf_counter() - start_time) * 1000.0

        start_time = time.perf_counter()
        res_pdf = fast_triage_extract(str(pdf_file), db=db, base_dir=str(tmp_path))
        elapsed_pdf_ms = (time.perf_counter() - start_time) * 1000.0

        threshold = 200.0 if _is_ci_or_parallel() else 50.0
        assert res_img == "[STATUS:PROVISIONAL]"
        assert res_pdf == "[STATUS:PROVISIONAL]"
        assert elapsed_img_ms < threshold, f"Image fast-path expected < {threshold}ms, got {elapsed_img_ms:.2f}ms"
        assert elapsed_pdf_ms < threshold, f"Low-conf PDF fast-path expected < {threshold}ms, got {elapsed_pdf_ms:.2f}ms"

        # Allow time for background jobs to run
        time.sleep(0.5)
    finally:
        db_worker.stop()


def test_in_memory_lru_sparse_matrix_cache_sub_5ms(tmp_path: Path):
    """Verify in-memory LRU matrix cache serves precomputed matrix structures in under 5ms."""
    db_worker = DBWorker()
    db_path = tmp_path / "lru_matrix.db"
    db = Database(db_path, db_worker)
    base_dir = str(tmp_path)

    try:
        db.upsert_document(base_dir, "fileA.txt", "hashA", "clinical trial patient data")
        db.upsert_document(base_dir, "fileB.txt", "hashB", "financial billing invoice tax")
        db.set_user_verified_target_path(base_dir, "fileA.txt", "Clinical/fileA.txt")
        db.set_user_verified_target_path(base_dir, "fileB.txt", "Finance/fileB.txt")

        db.update_tfidf_matrix_cache(base_dir)

        # Warm up in-memory LRU matrix cache
        db.get_tfidf_matrix_cache(base_dir)

        # Measure in-memory LRU cache retrieval speed
        start_time = time.perf_counter()
        cached_rows = db.get_tfidf_matrix_cache(base_dir)
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        threshold = 50.0 if _is_ci_or_parallel() else 5.0
        assert len(cached_rows) > 0
        assert elapsed_ms < threshold, f"Matrix cache lookup expected < {threshold}ms, got {elapsed_ms:.2f}ms"
    finally:
        db_worker.stop()


def test_lru_matrix_cache_500_doc_bounding_and_fallback():
    """Verify memory usage of in-memory LRU matrix cache stays bounded to 500 documents and handles fallbacks."""
    cache = SparseMatrixLRUCache(max_documents=500)

    # Populate 300 docs in dir1
    dir1_rows = [(f"dir1/doc_{i}.txt", "term", 1.0) for i in range(300)]
    cache.put("dir1", dir1_rows)
    assert cache.doc_count() == 300
    assert cache.get("dir1") is not None

    # Populate 250 docs in dir2 -> Total (300+250 = 550 > 500), should evict dir1
    dir2_rows = [(f"dir2/doc_{i}.txt", "term", 1.0) for i in range(250)]
    cache.put("dir2", dir2_rows)

    assert cache.doc_count() <= 500
    assert cache.get("dir1") is None  # Evicted!
    assert cache.get("dir2") is not None

    # Insert single oversized directory with 600 docs (> 500 max limit)
    dir3_rows = [(f"dir3/doc_{i}.txt", "term", 1.0) for i in range(600)]
    cache.put("dir3", dir3_rows)

    # Exceeds bounds, should not cache dir3 in memory
    assert cache.get("dir3") is None


def test_overall_pipeline_per_file_latency_under_150ms(tmp_path: Path):
    """Verify overall pipeline per-file latency remains below 150ms across document types."""
    files = {
        "text.txt": "Simple plain text document for fast classification.",
        "data.csv": "col1,col2\nval1,val2",
        "doc.md": "# Title\nSection content.",
    }

    latencies = []
    for filename, content in files.items():
        p = tmp_path / filename
        p.write_text(content, encoding="utf-8")

        start = time.perf_counter()
        _ = fast_triage_extract(str(p))
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        latencies.append(elapsed_ms)

    threshold = 200.0 if _is_ci_or_parallel() else 150.0
    for latency in latencies:
        assert latency < threshold, f"Pipeline latency expected < {threshold}ms, got {latency:.2f}ms"
