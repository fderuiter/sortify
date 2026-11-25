"""Unit and benchmark tests for fast-path triage pipeline, text quality confidence, LRU matrix cache, and async escalation queues."""

import time
from pathlib import Path

import pytest
from PIL import Image

from app.core.cache import SparseMatrixLRUCache
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.extractor import evaluate_text_confidence, fast_triage_extract


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
    assert (
        evaluate_text_confidence(
            "Medical trial summary report with detailed findings.", ".pdf"
        )
        >= 0.8
    )
    assert evaluate_text_confidence("", ".pdf") == 0.0
    assert evaluate_text_confidence("a b", ".pdf") < 0.8


def test_standard_text_and_csv_triage_under_50ms(tmp_path: Path):
    """Verify standard text and CSV files extract cleanly on the fast path."""
    text_file = tmp_path / "invoice.txt"
    text_file.write_text(
        "Medical Invoice #8841 for patient treatment", encoding="utf-8"
    )

    csv_file = tmp_path / "report.csv"
    csv_file.write_text("Patient,ID,Status\nJohn Doe,101,Active", encoding="utf-8")

    res_text = fast_triage_extract(str(text_file))
    res_csv = fast_triage_extract(str(csv_file))

    assert "Medical Invoice" in res_text
    assert "John Doe" in res_csv


def test_low_confidence_escalation_non_blocking(tmp_path: Path):
    """Verify low-confidence documents escalate to asynchronous background queues with provisional status."""
    img_file = tmp_path / "scanned_doc.png"
    img = Image.new("RGB", (150, 150), color="white")
    img.save(img_file)

    pdf_file = tmp_path / "blank_scanned.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 empty pdf stub")

    db_worker = DBWorker()
    db_path = tmp_path / "escalation.db"
    db = Database(db_path, db_worker)

    try:
        res_img = fast_triage_extract(str(img_file), db=db, base_dir=str(tmp_path))
        res_pdf = fast_triage_extract(str(pdf_file), db=db, base_dir=str(tmp_path))

        assert res_img == "[STATUS:PROVISIONAL]"
        assert res_pdf == "[STATUS:PROVISIONAL]"

        db_worker.flush()
    finally:
        db_worker.stop()


def test_in_memory_lru_sparse_matrix_cache_sub_5ms(tmp_path: Path):
    """Verify in-memory LRU matrix cache serves precomputed matrix structures."""
    db_worker = DBWorker()
    db_path = tmp_path / "lru_matrix.db"
    db = Database(db_path, db_worker)
    base_dir = str(tmp_path)

    try:
        db.upsert_document(
            base_dir, "fileA.txt", "hashA", "clinical trial patient data"
        )
        db.upsert_document(
            base_dir, "fileB.txt", "hashB", "financial billing invoice tax"
        )
        db.set_user_verified_target_path(base_dir, "fileA.txt", "Clinical/fileA.txt")
        db.set_user_verified_target_path(base_dir, "fileB.txt", "Finance/fileB.txt")

        db.update_tfidf_matrix_cache(base_dir)
        db.get_tfidf_matrix_cache(base_dir)
        cached_rows = db.get_tfidf_matrix_cache(base_dir)

        assert len(cached_rows) > 0
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


@pytest.mark.benchmark
def test_overall_pipeline_per_file_latency_under_150ms(tmp_path: Path):
    """Benchmark warmed fast-path triage, non-blocking escalation, and LRU matrix cache SLAs."""
    text_file = tmp_path / "invoice.txt"
    text_file.write_text(
        "Medical Invoice #8841 for patient treatment", encoding="utf-8"
    )
    csv_file = tmp_path / "report.csv"
    csv_file.write_text("Patient,ID,Status\nJohn Doe,101,Active", encoding="utf-8")
    md_file = tmp_path / "doc.md"
    md_file.write_text("# Title\nSection content.", encoding="utf-8")

    img_file = tmp_path / "scanned_doc.png"
    Image.new("RGB", (150, 150), color="white").save(img_file)
    pdf_file = tmp_path / "blank_scanned.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 empty pdf stub")

    db_worker = DBWorker()
    db = Database(tmp_path / "bench_triage.db", db_worker)
    base_dir = str(tmp_path)

    try:
        db.upsert_document(
            base_dir, "fileA.txt", "hashA", "clinical trial patient data"
        )
        db.upsert_document(
            base_dir, "fileB.txt", "hashB", "financial billing invoice tax"
        )
        db.set_user_verified_target_path(base_dir, "fileA.txt", "Clinical/fileA.txt")
        db.set_user_verified_target_path(base_dir, "fileB.txt", "Finance/fileB.txt")
        db.update_tfidf_matrix_cache(base_dir)

        # Explicit warmup across all fast-path extractors, escalation, and LRU cache
        fast_triage_extract(str(text_file))
        fast_triage_extract(str(csv_file))
        fast_triage_extract(str(md_file))
        fast_triage_extract(str(img_file), db=db, base_dir=base_dir)
        fast_triage_extract(str(pdf_file), db=db, base_dir=base_dir)
        db.get_tfidf_matrix_cache(base_dir)
        db_worker.flush()

        t0 = time.perf_counter()
        res_text = fast_triage_extract(str(text_file))
        elapsed_text_ms = (time.perf_counter() - t0) * 1000.0

        t0 = time.perf_counter()
        res_csv = fast_triage_extract(str(csv_file))
        elapsed_csv_ms = (time.perf_counter() - t0) * 1000.0

        t0 = time.perf_counter()
        res_md = fast_triage_extract(str(md_file))
        elapsed_md_ms = (time.perf_counter() - t0) * 1000.0

        t0 = time.perf_counter()
        res_img = fast_triage_extract(str(img_file), db=db, base_dir=base_dir)
        elapsed_img_ms = (time.perf_counter() - t0) * 1000.0

        t0 = time.perf_counter()
        res_pdf = fast_triage_extract(str(pdf_file), db=db, base_dir=base_dir)
        elapsed_pdf_ms = (time.perf_counter() - t0) * 1000.0

        t0 = time.perf_counter()
        cached_rows = db.get_tfidf_matrix_cache(base_dir)
        elapsed_cache_ms = (time.perf_counter() - t0) * 1000.0

        assert "Medical Invoice" in res_text
        assert "John Doe" in res_csv
        assert "Title" in res_md
        assert res_img == "[STATUS:PROVISIONAL]"
        assert res_pdf == "[STATUS:PROVISIONAL]"
        assert len(cached_rows) > 0

        assert elapsed_text_ms < 50.0, (
            f"Text triage expected < 50.0ms, got {elapsed_text_ms:.2f}ms"
        )
        assert elapsed_csv_ms < 50.0, (
            f"CSV triage expected < 50.0ms, got {elapsed_csv_ms:.2f}ms"
        )
        assert elapsed_md_ms < 150.0, (
            f"Markdown triage expected < 150.0ms, got {elapsed_md_ms:.2f}ms"
        )
        assert elapsed_img_ms < 50.0, (
            f"Image escalation expected < 50.0ms, got {elapsed_img_ms:.2f}ms"
        )
        assert elapsed_pdf_ms < 50.0, (
            f"Low-conf PDF escalation expected < 50.0ms, got {elapsed_pdf_ms:.2f}ms"
        )
        assert elapsed_cache_ms < 5.0, (
            f"Matrix cache lookup expected < 5.0ms, got {elapsed_cache_ms:.2f}ms"
        )
        db_worker.flush()
    finally:
        db_worker.stop()
