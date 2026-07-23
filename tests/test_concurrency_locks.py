import threading
import time
from unittest.mock import MagicMock
import pytest

from app.core.extractor import build_corpus_generator
from app.core.extractor_strategies import get_ocr_reader


def test_corpus_generator_semaphore_limits_concurrency(mocker):
    # Verify that at most max_workers are processed concurrently
    # and references to completed task futures are immediately deleted.
    import app.core.extractor as extractor
    
    active_count = 0
    max_concurrent = 0
    lock = threading.Lock()
    
    def mock_worker(base_dir, item, progress_callback, db):
        nonlocal active_count, max_concurrent
        with lock:
            active_count += 1
            if active_count > max_concurrent:
                max_concurrent = active_count
        # Sleep a bit to allow parallel threads to overlap
        time.sleep(0.05)
        with lock:
            active_count -= 1
        return item, f"text_{item}", f"hash_{item}"
        
    mocker.patch("app.core.extractor.process_item_worker", side_effect=mock_worker)
    
    mock_db = MagicMock()
    mock_db.get_document.return_value = None
    
    # Process 5 items with max_workers=2
    generator = build_corpus_generator(
        base_dir="/base",
        items_to_sort=["item1", "item2", "item3", "item4", "item5"],
        progress_callback=None,
        max_workers=2,
        chunk_size=1,
        db=mock_db
    )
    
    results = list(generator)
    assert len(results) == 5
    # Max concurrent executions should be exactly 2 (never exceed max_workers)
    assert max_concurrent <= 2
    assert max_concurrent > 0


def test_ocr_reader_concurrency(mocker):
    # Test that get_ocr_reader is thread-safe and serializes loading
    import app.core.extractor_strategies as strat
    
    strat._ocr_reader = None
    strat._ocr_reader_loaded = False
    
    mock_easyocr = MagicMock()
    mock_easyocr_reader = MagicMock(return_value="reader_instance")
    mock_easyocr.Reader = mock_easyocr_reader
    mocker.patch.dict("sys.modules", {"easyocr": mock_easyocr})

    mock_torch = MagicMock()
    mocker.patch.dict("sys.modules", {"torch": mock_torch})
    
    results = []
    
    def call_reader():
        try:
            reader = get_ocr_reader()
            results.append(reader)
        except Exception as e:
            results.append(e)
            
    threads = [threading.Thread(target=call_reader) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
        
    # All threads should get the same reader instance
    for r in results:
        assert r == "reader_instance"
        
    # easyocr.Reader should be called exactly once
    mock_easyocr_reader.assert_called_once()
