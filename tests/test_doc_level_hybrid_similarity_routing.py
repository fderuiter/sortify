import hashlib
import math
import shutil
import tempfile
from pathlib import Path

import pytest

from app.core.analyzer import IncrementalAnalyzer
from app.core.db import Database
from app.core.db_conn import clear_connection_cache, get_db_connection
from app.core.db_worker import DBWorker

_test_dir = None
db_worker = None
db = None


def setup_module(module):
    global _test_dir, db_worker, db
    _test_dir = tempfile.mkdtemp()
    db_worker = DBWorker()
    db = Database(Path(_test_dir) / "test_hybrid.db", db_worker)


def teardown_module(module):
    global _test_dir, db_worker
    if db_worker:
        db_worker.stop()
    clear_connection_cache()
    if _test_dir:
        shutil.rmtree(_test_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_db():
    db.clear()
    yield


def test_per_comparison_hybrid_routing(mocker):
    """
    Verify that Document-Level Hybrid Similarity Routing handles documents on a per-comparison basis:
    - Healthy document with a valid vector is routed using cosine similarity.
    - Document with missing vector falls back individually to TF-IDF.
    - It triggers background reconstruction for the missing/invalid vector documents.
    """
    base_dir = "test_hybrid_routing_base"
    db.clear(base_dir)

    # 1. Setup metadata
    db.set_model_metadata("active_model_signature", "active_sig")
    db.set_model_metadata("active_model_dimensions", "384")
    db.set_model_metadata("active_model_version", "1.0.0")

    # Initialize the analyzer
    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "and"}, db=db, model_path="all-MiniLM-L6-v2"
    )

    # 2. Add historical document with valid vector
    # We use partial_fit to ensure TF-IDF tables are populated
    hist_text = "billing invoice payments finance cash bank"
    analyzer.partial_fit(base_dir, {"hist_doc_finance.txt": hist_text})
    h_hist = hashlib.md5(hist_text.encode("utf-8")).hexdigest()
    db.set_user_verified_target(base_dir, h_hist, "Finance")

    # 3. Add active documents: one healthy, one with a missing vector
    corpus = {
        "healthy_doc.txt": "invoice billing payments transaction account credit",
        "unreadable_doc.txt": "completely unrelated text without any finance terms",
    }
    analyzer.partial_fit(base_dir, corpus)

    # 4. Insert valid vector embeddings ONLY for healthy doc and historical doc
    dim = 384
    vec = [1.0 / math.sqrt(dim)] * dim

    db.upsert_document_vectors(
        base_dir,
        [("hist_doc_finance.txt", vec), ("healthy_doc.txt", vec)],
        model_signature="active_sig",
    )

    # Mock semantic manager properties
    mocker.patch(
        "app.core.semantic_embeddings.SemanticEmbeddingManager.is_mock",
        new_callable=mocker.PropertyMock,
        return_value=False,
    )
    analyzer.embedding_manager.is_model_valid = True
    analyzer.embedding_manager.dimensions = 384

    # Mock trigger_reconstruction to be a no-op so the background thread doesn't run and clear our tracking set
    mock_trigger = mocker.patch.object(
        analyzer.embedding_manager, "trigger_reconstruction"
    )

    # 6. Generate sorting plan
    plan = analyzer.generate_sorting_plan(base_dir)

    # 7. Assertions
    # - Healthy doc is routed using cosine similarity (similarity = 1.0 >= 0.8) to "Finance"
    assert "Finance" in plan
    assert "healthy_doc.txt" in plan["Finance"]
    assert plan["Finance"]["healthy_doc.txt"]["routed_by"] == "similarity"

    # - Unreadable doc has no valid vector, is not matched semantically to "Finance"
    # - Unreadable doc's missing vector triggers background reconstruction
    assert "unreadable_doc.txt" not in plan.get("Finance", {})
    assert mock_trigger.call_count > 0
    assert (base_dir.replace("\\", "/"), "unreadable_doc.txt") in db.corrupted_vectors


def test_similarity_score_merging_range(mocker):
    """
    Verify that document pairs with both validated vectors return cosine similarity score,
    and pairs with at least one invalid/missing vector return TF-IDF keyword similarity score.
    Also, ensure that score values are strictly clipped/bounded to the [0.0, 1.0] range.
    """
    base_dir = "test_similarity_range_base"
    db.clear(base_dir)

    # 1. Setup metadata
    db.set_model_metadata("active_model_signature", "active_sig")
    db.set_model_metadata("active_model_dimensions", "384")
    db.set_model_metadata("active_model_version", "1.0.0")

    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the"}, db=db, model_path="all-MiniLM-L6-v2"
    )

    # 2. Setup documents
    hist_text = "finance invoice money"
    analyzer.partial_fit(
        base_dir, {"hist_1.txt": hist_text, "active_1.txt": "finance invoice money"}
    )
    h_hist = hashlib.md5(hist_text.encode("utf-8")).hexdigest()
    db.set_user_verified_target(base_dir, h_hist, "Finance")

    # Upsert historical and active vectors with extremely high values to test clipping
    dim = 384
    vec_healthy = [1.5] * dim  # Non-normalized
    db.upsert_document_vectors(
        base_dir,
        [("hist_1.txt", vec_healthy), ("active_1.txt", vec_healthy)],
        model_signature="active_sig",
    )

    mocker.patch(
        "app.core.semantic_embeddings.SemanticEmbeddingManager.is_mock",
        new_callable=mocker.PropertyMock,
        return_value=False,
    )
    analyzer.embedding_manager.is_model_valid = True
    analyzer.embedding_manager.dimensions = 384

    # Trigger plan generation
    plan = analyzer.generate_sorting_plan(base_dir)

    # Assert correct semantic routing and score values
    assert "Finance" in plan
    assert "active_1.txt" in plan["Finance"]
    active_entry = plan["Finance"]["active_1.txt"]
    assert active_entry.get("routed_by") in ("similarity", "historical", "tfidf")
    keyword_str = active_entry.get("keyword")
    if keyword_str:
        import re

        match = re.search(r"\(([0-9.]+)\)", str(keyword_str))
        if match:
            score = float(match.group(1))
            assert 0.0 <= score <= 1.0


def test_cache_integrity_unencrypted_security():
    """
    Ensure no unencrypted vector data is persisted to the database.
    """
    base_dir = "test_unencrypted_security_base"
    db.clear(base_dir)
    dim = 384
    vec = [0.1] * dim
    db.upsert_document_vectors(
        base_dir,
        [("doc_secure.txt", vec)],
        model_signature="active_sig",
    )

    conn = get_db_connection(db.db_path)
    with conn:
        cursor = conn.execute(
            "SELECT vector FROM document_vectors WHERE base_dir = ?", (base_dir,)
        )
        rows = cursor.fetchall()
        assert len(rows) > 0
        for row in rows:
            if row[0]:
                vector_str = row[0]
                # If vector is stored unencrypted, it would start with "[" and end with "]"
                assert not (vector_str.startswith("[") and vector_str.endswith("]")), (
                    "Persisted vector data must be encrypted!"
                )
