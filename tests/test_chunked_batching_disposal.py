"""Tests for Chunked Generator Batching with Payload Disposal."""

from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings
from app.core.analyzer import IncrementalAnalyzer
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.extractor import build_corpus_generator, build_corpus_generator_async


def test_batch_chunking_50_item_increments(tmp_path):
    """Verify that document collections are submitted for worker execution in 50-item increments."""
    base_dir = tmp_path / "test_base"
    base_dir.mkdir()

    # Create 120 mock file names
    filenames = [f"doc_{i:03d}.txt" for i in range(120)]
    for fname in filenames:
        (base_dir / fname).write_text(f"Content for {fname}")

    db_worker = DBWorker()
    db = Database(tmp_path / "test.db", db_worker)
    settings = AppSettings()

    submitted_batches = []

    def mock_process_item_worker(base_dir, item, progress_cb, db, settings=None):
        return item, f"Extracted text for {item}", f"hash_{item}"

    with patch(
        "app.core.extractor.process_item_worker", side_effect=mock_process_item_worker
    ):
        generator = build_corpus_generator(
            base_dir=str(base_dir),
            items_to_sort=filenames,
            progress_callback=MagicMock(),
            max_workers=4,
            db=db,
            chunk_size=50,
            sequential=False,
            settings=settings,
        )

        chunks = list(generator)

    # 120 files in 50-item chunks should produce 3 chunks (50, 50, 20)
    assert len(chunks) == 3
    assert len(chunks[0]) == 50
    assert len(chunks[1]) == 50
    assert len(chunks[2]) == 20

    db_worker.stop()


def test_raw_text_payload_disposal(tmp_path):
    """Verify that raw document text strings are purged from analyzer memory after extraction."""
    db_worker = DBWorker()
    db = Database(tmp_path / "test.db", db_worker)
    analyzer = IncrementalAnalyzer(max_folders=3, stop_words=set(), db=db)

    large_text = "X" * 100_000  # 100KB string payload
    corpus_chunk = {
        "file1.txt": {"text": large_text, "hash": "hash1"},
        "file2.txt": {"text": large_text, "hash": "hash2"},
    }

    analyzer.partial_fit(str(tmp_path), corpus_chunk)

    # Verify keys exist in analyzer.corpus but mapped to None (not holding raw text)
    assert len(analyzer.corpus) == 2
    assert "file1.txt" in analyzer.corpus
    assert "file2.txt" in analyzer.corpus
    assert analyzer.corpus["file1.txt"] is None
    assert analyzer.corpus["file2.txt"] is None

    db_worker.stop()


def test_large_directory_5000_files_no_queue_saturation(tmp_path):
    """Verify that processing a directory of 5,000 files completes without queue saturation."""
    base_dir = tmp_path / "large_dir"
    base_dir.mkdir()

    filenames = [f"file_{i:04d}.txt" for i in range(5000)]

    db_worker = DBWorker()
    db = Database(tmp_path / "test.db", db_worker)
    settings = AppSettings()

    def mock_process_item_worker(base_dir, item, progress_cb, db, settings=None):
        return item, f"text_{item}", f"hash_{item}"

    with patch(
        "app.core.extractor.process_item_worker", side_effect=mock_process_item_worker
    ):
        generator = build_corpus_generator(
            base_dir=str(base_dir),
            items_to_sort=filenames,
            progress_callback=MagicMock(),
            max_workers=4,
            db=db,
            chunk_size=50,
            sequential=False,
            settings=settings,
        )

        total_processed = 0
        chunk_count = 0
        for chunk in generator:
            chunk_count += 1
            total_processed += len(chunk)
            # Maximum size of any yielded chunk should be <= 50
            assert len(chunk) <= 50

    assert total_processed == 5000
    assert chunk_count == 100  # 5000 / 50 = 100 chunks

    db_worker.stop()


@pytest.mark.anyio
async def test_async_generator_50_item_batching(tmp_path):
    """Verify build_corpus_generator_async processes in 50-item batches sequentially."""
    base_dir = tmp_path / "async_base"
    base_dir.mkdir()

    filenames = [f"async_file_{i:03d}.txt" for i in range(110)]
    for fname in filenames:
        (base_dir / fname).write_text(f"Async content {fname}")

    db_worker = DBWorker()
    db = Database(tmp_path / "test.db", db_worker)
    settings = AppSettings()

    results = []
    gen = build_corpus_generator_async(
        base_dir=str(base_dir),
        items_to_sort=filenames,
        db=db,
        settings=settings,
        batch_size=50,
    )

    async for item, text, fhash, was_skipped in gen:
        results.append((item, text, fhash, was_skipped))

    assert len(results) == 110
    assert results[0][0] == "async_file_000.txt"

    db_worker.stop()
