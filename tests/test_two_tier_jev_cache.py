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


def test_single_query_cache_miss(tmp_path: Path):
    """Verify _get_from_db executes only a single query on a cache miss."""
    db_path = str(tmp_path / "autosorter.db")
    engine = JevClassifierEngine(db_path=db_path)

    test_file = tmp_path / "uncached_doc.txt"
    test_file.write_text("Some uncached content.")
    stat_key = engine._get_file_stat_key(str(test_file))
    assert stat_key is not None

    from app.core.db_conn import get_db_connection

    real_conn = get_db_connection(db_path)

    class ProxyConnection:
        def __init__(self, conn):
            self._conn = conn
            self.executed_queries = []

        def execute(self, sql, *args, **kwargs):
            self.executed_queries.append(str(sql))
            return self._conn.execute(sql, *args, **kwargs)

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return self._conn.__exit__(exc_type, exc_val, exc_tb)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    proxy_conn = ProxyConnection(real_conn)

    with patch("app.core.db_conn.get_db_connection", return_value=proxy_conn):
        res = engine._get_from_db(stat_key)

    assert res is None
    # Confirm exactly 1 SELECT query was executed during DB lookup
    select_queries = [q for q in proxy_conn.executed_queries if "SELECT" in q.upper()]
    assert len(select_queries) == 1
    assert "WHERE file_path = ?" in select_queries[0]


def test_stale_entry_invalidation_and_corrupted_metadata(tmp_path: Path):
    """Verify stale entry with mismatched mtime/size invalidates via single lookup and handles bad metadata gracefully."""
    db_path = str(tmp_path / "autosorter.db")
    engine = JevClassifierEngine(db_path=db_path)

    test_file = tmp_path / "stale_doc.txt"
    test_file.write_text("Fresh content.")
    stat_key = engine._get_file_stat_key(str(test_file))
    assert stat_key is not None
    abs_path, mtime, size = stat_key

    from app.core.db_conn import get_db_connection

    conn = get_db_connection(db_path)

    # Insert a stale row with old mtime and size
    with conn:
        conn.execute(
            """INSERT INTO jev_classification_cache (
                file_path, mtime, size, category, sensitivity_rating,
                sensitivity_score, archival_priority, archival_priority_score,
                confidence, is_classified, metadata, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                abs_path,
                mtime - 100.0,
                size + 50,
                "Old Category",
                "LOW",
                0.1,
                5,
                0.1,
                0.5,
                1,
                "{invalid json",
                time.time(),
            ),
        )

    # _get_from_db should perform single query lookup, detect stale mtime/size, and invalidate
    res = engine._get_from_db(stat_key)
    assert res is None

    # Verify stale row was deleted from DB
    with conn:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM jev_classification_cache WHERE file_path = ?",
            (abs_path,),
        )
        assert cursor.fetchone()[0] == 0

    # Insert a valid row with matching stat key but corrupted metadata JSON
    with conn:
        conn.execute(
            """INSERT INTO jev_classification_cache (
                file_path, mtime, size, category, sensitivity_rating,
                sensitivity_score, archival_priority, archival_priority_score,
                confidence, is_classified, metadata, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                abs_path,
                mtime,
                size,
                "Financial Reports",
                "HIGH",
                0.85,
                1,
                0.90,
                0.95,
                1,
                "{corrupted json metadata",
                time.time(),
            ),
        )

    res2 = engine._get_from_db(stat_key)
    assert res2 is not None
    assert res2.category == "Financial Reports"
    assert res2.metadata == {}  # Graceful fallback to empty dict
