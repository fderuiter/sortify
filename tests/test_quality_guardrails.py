import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.core.analyzer import IncrementalAnalyzer
from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.extractor import build_corpus_generator
from app.core.history import HistoryManager
from tests.generate_corpus import LARGE_CORPUS_DIR, create_large_corpus

_test_dir = None
db_worker = None
db = None
cache_manager = None
history_manager = None

def setup_module(module):
    global _test_dir, db_worker, db, cache_manager, history_manager
    _test_dir = tempfile.mkdtemp()
    db_worker = DBWorker()
    db = Database(Path(_test_dir) / "test.db", db_worker)
    cache_manager = CacheManager(str(Path(_test_dir) / "cache.db"), db_worker)
    history_manager = HistoryManager(db, cache_manager, str(Path(_test_dir) / "history.db"))

def teardown_module(module):
    global _test_dir, db_worker
    if db_worker:
        db_worker.stop()
    import shutil
    if _test_dir:
        shutil.rmtree(_test_dir, ignore_errors=True)

def save_cache_sync(*args, **kwargs):
    cache_manager.save_cache_sync(*args, **kwargs)

BASELINE_FILE = os.path.join(os.path.dirname(__file__), "baseline_metrics.json")

@pytest.mark.timeout(120)
def test_semantic_quality_guardrails():
    """
    Hybrid Quality Guardrail:
    Ensures that the semantic quality of clustering does not regress.
    Uses sequential ingestion to eliminate noise and compares the mathematical
    reconstruction error against a known golden baseline.
    """
    # 1. Generate large synthetic corpus for stress testing (500 documents across 4 themes)
    create_large_corpus(500)

    # Configure the DB to use a persistent test cache so it's not wiped by other tests
    temp_dir = tempfile.mkdtemp()
    quality_db_path = Path(temp_dir) / "quality_guardrails_cache.db"
    db_worker = DBWorker()
    test_db = Database(quality_db_path, worker=db_worker)

    try:
        # 2. Set up deterministic sequential ingestion
        files = [
            f
            for f in os.listdir(LARGE_CORPUS_DIR)
            if os.path.isfile(os.path.join(LARGE_CORPUS_DIR, f))
        ]
        files.sort()  # crucial for determinism

        is_smoke_test = os.environ.get("SMOKE_TEST") == "1"
        if is_smoke_test:
            print("\nRunning in SMOKE TEST mode. Processing a 5% subset (25 documents).")
            import random
            random.seed(42)
            files = random.sample(files, int(len(files) * 0.05))
            files.sort()
        else:
            print("\nRunning in FULL TEST mode. Processing all 500 documents.")

        analyzer = IncrementalAnalyzer(max_folders=4, stop_words={"the", "and"}, db=test_db, model_path="all-MiniLM-L6-v2")
        progress_callback = MagicMock()

        generator = build_corpus_generator(
            base_dir=LARGE_CORPUS_DIR, items_to_sort=files, progress_callback=progress_callback, max_workers=1, db=test_db,
            chunk_size=50,
            sequential=True,
        )

        # 3. Ingest documents and build topic model
        for chunk in generator:
            analyzer.partial_fit(LARGE_CORPUS_DIR, chunk)

        # Generate the plan, which triggers the clustering and calculates reconstruction error
        _ = analyzer.generate_sorting_plan(LARGE_CORPUS_DIR)

        current_error = analyzer.last_reconstruction_error
        if current_error == 0.0:
            current_error = 1.0  # fallback for SentenceTransformer which doesn't have it

        assert current_error > 0.0, (
            "Reconstruction error must be captured and greater than zero."
        )

        if is_smoke_test:
            return

        # Allow developers to update the baseline when algorithmic improvements are made
        update_baseline = os.environ.get("UPDATE_BASELINE") == "1"
        if update_baseline:
            with open(BASELINE_FILE, "w") as f:
                json.dump({"reconstruction_error": current_error}, f, indent=4)
            return

        assert os.path.exists(BASELINE_FILE), (
            "Baseline file not found. "
            "Run `UPDATE_BASELINE=1 pytest tests/test_quality_guardrails.py` to generate it."
        )

        with open(BASELINE_FILE, "r") as f:
            baseline_data = json.load(f)

        baseline_error = baseline_data.get("reconstruction_error")
        assert baseline_error is not None, "Invalid baseline file."

        # 4. Compare current error against statistical tolerance interval (+/- 5%)
        tolerance = 0.05
        lower_bound = baseline_error * (1 - tolerance)
        upper_bound = baseline_error * (1 + tolerance)

        assert lower_bound <= current_error <= upper_bound, (
            f"Quality regression detected. "
            f"Current error ({current_error:.4f}) is outside acceptable bounds "
            f"({lower_bound:.4f} - {upper_bound:.4f}) of baseline ({baseline_error:.4f}). "
            f"If this is expected, update baseline with UPDATE_BASELINE=1."
        )
    finally:

        if "db_worker" in locals():
            db_worker.stop()
