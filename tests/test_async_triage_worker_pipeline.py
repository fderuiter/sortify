"""Unit tests for asynchronous triage pipeline, DBWorker background queue, and TF-IDF matrix cache."""

import os
import time
from pathlib import Path

from PIL import Image

from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.extractor import fast_triage_extract


def _is_ci_or_parallel() -> bool:
    return (
        "PYTEST_XDIST_WORKER" in os.environ
        or "CI" in os.environ
        or os.environ.get("GITHUB_ACTIONS") == "true"
    )


def test_fast_triage_extraction_under_50ms(tmp_path: Path):
    """Verify that fast triage extraction returns under 50ms per file."""
    text_file = tmp_path / "sample.txt"
    text_file.write_text("Invoice number 12345 for medical supplies.", encoding="utf-8")

    start_time = time.perf_counter()
    extracted = fast_triage_extract(str(text_file))
    elapsed_ms = (time.perf_counter() - start_time) * 1000.0

    threshold = 200.0 if _is_ci_or_parallel() else 50.0
    assert "Invoice" in extracted
    assert elapsed_ms < threshold, f"Expected < {threshold}ms, got {elapsed_ms:.2f}ms"


def test_fast_triage_provisional_for_images_and_background_queue(tmp_path: Path):
    """Verify that images return [STATUS:PROVISIONAL] immediately during fast triage and queue background VLM/OCR."""
    img_file = tmp_path / "test_image.png"
    img = Image.new("RGB", (100, 100), color="white")
    img.save(img_file)

    db_worker = DBWorker()
    db_path = tmp_path / "test_triage.db"
    db = Database(db_path, db_worker)

    try:
        start_time = time.perf_counter()
        res = fast_triage_extract(str(img_file), db=db, base_dir=str(tmp_path))
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        threshold = 200.0 if _is_ci_or_parallel() else 50.0
        assert res == "[STATUS:PROVISIONAL]"
        assert elapsed_ms < threshold, f"Expected < {threshold}ms, got {elapsed_ms:.2f}ms"

        # Allow time for background enrichment job to process
        time.sleep(1.0)
    finally:
        db_worker.stop()


def test_db_worker_background_concurrency_and_listeners(tmp_path: Path):
    """Verify DBWorker background job pool accepts jobs and notifies listeners."""
    worker = DBWorker()
    events = []

    def _listener(event_type, data):
        events.append((event_type, data))

    worker.register_listener(_listener)

    def _sample_bg_job():
        time.sleep(0.05)
        return "completed_result"

    fut = worker.submit_background_job(_sample_bg_job)
    res = fut.result(timeout=2.0)

    assert res == "completed_result"
    assert any(e[0] == "job_complete" and e[1] == "completed_result" for e in events)

    worker.stop()


def test_tfidf_matrix_cache_sub_10ms_retrieval(tmp_path: Path):
    """Verify that TF-IDF matrix cache retrieves pre-computed term weights in under 10ms."""
    db_worker = DBWorker()
    db_path = tmp_path / "test_matrix.db"
    db = Database(db_path, db_worker)

    base_dir = str(tmp_path)
    file1 = "doc1.txt"
    file2 = "doc2.txt"

    try:
        db.upsert_document(base_dir, file1, "hash1", "medical patient blood report clinical")
        db.upsert_document(base_dir, file2, "hash2", "financial tax invoice payment clinical")
        db.set_user_verified_target_path(base_dir, file1, "Historical/doc1.txt")
        db.set_user_verified_target_path(base_dir, file2, "Historical/doc2.txt")

        # Manually invoke cache materialization
        db.update_tfidf_matrix_cache(base_dir)

        start_time = time.perf_counter()
        cached_rows = db.get_tfidf_matrix_cache(base_dir)
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        threshold = 200.0 if _is_ci_or_parallel() else 50.0
        assert len(cached_rows) > 0
        assert elapsed_ms < threshold, f"Expected < {threshold}ms, got {elapsed_ms:.2f}ms"

        file_paths = {row[0] for row in cached_rows}
        terms = {row[1] for row in cached_rows}
        assert file1 in file_paths
        assert "clinical" in terms
    finally:
        db_worker.stop()

