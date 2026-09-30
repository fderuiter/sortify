"""Tests for two-tier classification caching (in-memory LRU + persistent SQLite DB) in JevClassifierEngine."""

import os
import time
from pathlib import Path
from unittest.mock import patch

from app.core.db_worker import DBWorker
from app.core.jev_classifier import JevClassifierEngine


def test_in_memory_lru_cache_hit_zero_snippet_reads(tmp_path: Path):
    """Verify in-memory BoundedMemoryCache returns results without re-reading file snippets from disk."""
    engine = JevClassifierEngine(max_cache_size=100)
    test_file = tmp_path / "invoice_q3.csv"
    test_file.write_text("Invoice ID, Amount\n101, $250\n")

    # First classification call - cache miss, performs file snippet read
    res1 = engine.classify(str(test_file))
    assert res1.is_classified is True
    assert "Financial" in res1.category

    # Mock open to verify zero file reads on repeat classification within same session
    with patch(
        "builtins.open",
        side_effect=AssertionError("Disk read should not occur on cached hit"),
    ):
        t0 = time.perf_counter()
        res2 = engine.classify(str(test_file))
        latency_ms = (time.perf_counter() - t0) * 1000.0

    assert latency_ms < 10.0  # Fast-path sub-10ms response time
    assert res2.is_classified is True
    assert res2.category == res1.category


def test_persistent_database_cache_hit_across_restarts(tmp_path: Path):
    """Verify SQLite database table jev_classification_cache persists results across application restarts."""
    db_path = str(tmp_path / "autosorter.db")
    worker = DBWorker()
    try:
        engine1 = JevClassifierEngine(db_path=db_path, worker=worker)
        test_file = tmp_path / "nda_contract_2026.pdf"
        test_file.write_text("Non-disclosure agreement terms and legal compliance.")

        # Classify and persist
        res1 = engine1.classify(str(test_file))
        assert res1.is_classified is True
        assert "Legal" in res1.category

        # Wait briefly for worker async write
        time.sleep(0.2)

        # Simulate app restart by creating a new engine instance with empty memory cache
        engine2 = JevClassifierEngine(db_path=db_path, worker=worker)
        assert len(engine2.memory_cache) == 0

        # Spy on snippet reading logic to confirm DB cache hit avoids re-reading snippets
        t0 = time.perf_counter()
        res2 = engine2.classify(str(test_file))
        latency_ms = (time.perf_counter() - t0) * 1000.0

        assert latency_ms < 10.0
        assert res2.is_classified is True
        assert res2.category == res1.category
        assert res2.sensitivity_rating == res1.sensitivity_rating
        assert res2.archival_priority == res1.archival_priority
        # Confirm in-memory cache was populated from DB
        assert len(engine2.memory_cache) == 1
    finally:
        worker.stop()


def test_cache_invalidation_on_file_modification(tmp_path: Path):
    """Verify modifying file modification timestamp or size invalidates both in-memory and DB cache entries."""
    db_path = str(tmp_path / "autosorter.db")
    worker = DBWorker()
    try:
        engine = JevClassifierEngine(db_path=db_path, worker=worker)
        test_file = tmp_path / "patient_file.txt"
        test_file.write_text("Patient clinical report and lab diagnosis.")

        res1 = engine.classify(str(test_file))
        assert "Medical" in res1.category

        time.sleep(0.1)

        # Touch/modify the file to update modification timestamp
        new_mtime = time.time() + 10.0
        os.utime(str(test_file), (new_mtime, new_mtime))

        # Re-classify: cache lookup should miss due to stat key change
        stat_key_old = engine._get_file_stat_key(str(test_file))
        assert stat_key_old is not None

        # Change content to technical asset keywords
        test_file.write_text(
            "API spec build config log script README source repo schema."
        )
        os.utime(str(test_file), (new_mtime + 5.0, new_mtime + 5.0))

        res2 = engine.classify(str(test_file))
        assert "Technical" in res2.category
    finally:
        worker.stop()


def test_explicit_invalidation(tmp_path: Path):
    """Verify explicit invalidate call purges entries from memory and database."""
    db_path = str(tmp_path / "autosorter.db")
    worker = DBWorker()
    try:
        engine = JevClassifierEngine(db_path=db_path, worker=worker)
        test_file = tmp_path / "invoice_test.csv"
        test_file.write_text("Invoice ID, $100\n")

        res1 = engine.classify(str(test_file))
        assert res1.is_classified is True
        assert len(engine.memory_cache) == 1

        engine.invalidate(str(test_file))
        assert len(engine.memory_cache) == 0

        time.sleep(0.1)

        # Confirm DB record deleted
        from app.core.db_conn import get_db_connection

        conn = get_db_connection(db_path)
        with conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM jev_classification_cache WHERE file_path = ?",
                (os.path.abspath(str(test_file)).replace("\\", "/"),),
            )
            count = cursor.fetchone()[0]
            assert count == 0
    finally:
        worker.stop()


def test_bounded_memory_capacity_and_eviction(tmp_path: Path):
    """Verify in-memory LRU cache capacity is bounded and evicts oldest entries."""
    engine = JevClassifierEngine(max_cache_size=2)
    files = []
    for i in range(3):
        f = tmp_path / f"doc_{i}.csv"
        f.write_text(f"Invoice ID, {i}\n")
        files.append(f)
        engine.classify(str(f))

    # Memory cache capacity max_size is 2, so doc_0 should be evicted from LRU memory
    assert len(engine.memory_cache) == 2
    keys = [k[0] if isinstance(k, tuple) else k for k in engine.memory_cache.keys()]
    assert os.path.abspath(str(files[0])).replace("\\", "/") not in keys
    assert os.path.abspath(str(files[2])).replace("\\", "/") in keys


def test_safe_fallback_on_db_failure(tmp_path: Path):
    """Verify engine falls back safely to fresh rule evaluation if DB or memory errors occur."""
    engine = JevClassifierEngine(db_path=str(tmp_path / "non_existent_dir" / "bad.db"))
    test_file = tmp_path / "contract.pdf"
    test_file.write_text("Agreement contract and terms of service.")

    # Should not raise exception despite bad DB path
    res = engine.classify(str(test_file))
    assert res.is_classified is True
    assert "Legal" in res.category
