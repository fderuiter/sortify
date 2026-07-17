import numpy as np

from app.core.analyzer import IncrementalAnalyzer
from app.core.db import Database
from app.core.db_worker import DBWorker


def test_find_similar(tmp_path):
    class MockFuture:
        def result(self):
            return [np.array([1.0, 0.0, 0.0], dtype=np.float32)]

    class MockExecutor:
        def submit(self, fn, *args, **kwargs):
            return MockFuture()

    db_worker = DBWorker()
    db = Database(tmp_path / "test_docs.db", worker=db_worker)
    
    analyzer = IncrementalAnalyzer(max_folders=5, stop_words=set(), db=db)
    analyzer.executor = MockExecutor()
    analyzer.model_name = "test_model"
    analyzer._cached_dimension = 3

    # Insert dummy records
    db.upsert_document(
        str(tmp_path),
        "doc1.txt",
        "hash1",
        "doc1",
        np.array([1.0, 0.0, 0.0]),
        "test_model",
        3,
    )
    db.upsert_document(
        str(tmp_path),
        "doc2.txt",
        "hash2",
        "doc2",
        np.array([0.0, 1.0, 0.0]),
        "test_model",
        3,
    )
    db.upsert_document(
        str(tmp_path),
        "doc3.txt",
        "hash3",
        "doc3",
        np.array([0.5, 0.5, 0.0]),
        "test_model",
        3,
    )

    results = analyzer.find_similar(str(tmp_path), "query", top_k=2)

    assert len(results) == 2
    assert results[0]["filepath"] == "doc1.txt"
    assert results[0]["similarity"] > 0.99
    assert results[1]["filepath"] == "doc3.txt"
    assert results[1]["similarity"] > 0.7  # cos(45 deg) ~ 0.707
    db_worker.stop()
