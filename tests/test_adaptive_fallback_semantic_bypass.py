import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.analyzer import IncrementalAnalyzer
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.semantic_embeddings import ModelProperties, SemanticEmbeddingManager


class MockSettings:
    MAX_FOLDERS = 5
    STOP_WORDS = {"the", "and"}
    AI_CONSENT_GRANTED = True


@pytest.fixture
def temp_dir():
    dir_path = tempfile.mkdtemp()
    yield Path(dir_path)
    from app.core.db_conn import clear_connection_cache

    clear_connection_cache()
    shutil.rmtree(dir_path, ignore_errors=True)


@pytest.fixture
def db_worker():
    worker = DBWorker()
    yield worker
    worker.stop()


@pytest.fixture
def db(temp_dir, db_worker):
    database = Database(temp_dir / "test.db", db_worker)
    yield database
    from app.core.db_conn import clear_connection_cache

    clear_connection_cache()


def test_mock_state_detection(db, temp_dir):
    """
    Requirement 1: System must detect if the active semantic engine is running in mock state
    without a physical model file.
    """
    # Case A: model_path is None -> must be mock
    manager_none = SemanticEmbeddingManager(db, model_path=None)
    assert manager_none.is_mock is True

    # Case B: model_path is a non-existent path -> must be mock
    non_existent_path = temp_dir / "non_existent_model"
    manager_non_existent = SemanticEmbeddingManager(
        db, model_path=str(non_existent_path)
    )
    assert manager_non_existent.is_mock is True

    # Case C: model_path exists but is empty (no ONNX file) -> must be mock
    empty_model_dir = temp_dir / "empty_model_dir"
    empty_model_dir.mkdir(parents=True, exist_ok=True)
    manager_empty = SemanticEmbeddingManager(db, model_path=str(empty_model_dir))
    assert manager_empty.is_mock is True


def test_statistical_fallback_on_mock(db, temp_dir):
    """
    Requirement 2: The system must switch document categorization to statistical text similarity
    when mock state is detected.
    Requirement 4: No background vector reconstruction loops trigger when the bypass is active.
    """
    # Create IncrementalAnalyzer with no model_path so it is in mock state
    analyzer = IncrementalAnalyzer(
        max_folders=3,
        stop_words={"the", "and"},
        db=db,
        strategy_name="generative",  # Set to generative to test fallback to default
        model_path=None,
    )

    # Prepare some documents
    base_dir = str(temp_dir)
    db.clear(base_dir)

    # Verified document with structured text
    hist_text = "space rockets Mars landing astronauts and cosmic deep space exploration mission"
    db.upsert_document(base_dir, "historical_space.txt", "hash_h", hist_text)
    db.set_user_verified_target(base_dir, "hash_h", "Space")

    # New unverified document - almost identical to guarantee high statistical similarity
    new_text = (
        "space rockets Mars landing astronauts and cosmic deep space exploration flight"
    )
    corpus = {"new_space.txt": new_text}
    analyzer.partial_fit(base_dir, corpus)

    # Let's verify analyzer is running on mock
    assert analyzer.embedding_manager.is_mock is True

    # Spy on generate_embedding and trigger_reconstruction to ensure they aren't called
    with (
        patch.object(
            analyzer.embedding_manager,
            "generate_embedding",
            wraps=analyzer.embedding_manager.generate_embedding,
        ) as mock_gen,
        patch.object(
            analyzer.embedding_manager,
            "trigger_reconstruction",
            wraps=analyzer.embedding_manager.trigger_reconstruction,
        ) as mock_reconstruct,
    ):
        plan = analyzer.generate_sorting_plan(base_dir)

        # 1. Similarity matching should have run using statistical TF-IDF similarity.
        # Since new_text and hist_text are extremely similar, it should match to "Space" (similarity >= 0.8)
        assert "Space" in plan
        assert "new_space.txt" in plan["Space"]
        assert plan["Space"]["new_space.txt"]["routed_by"] == "similarity"

        # 2. Embedding generation & background vector reconstruction loops must NOT be triggered
        mock_gen.assert_not_called()
        mock_reconstruct.assert_not_called()
        assert analyzer.embedding_manager.is_reconstruction_active() is False


def test_dynamic_transition_to_semantic(db, temp_dir):
    """
    Requirement 3: The system must re-enable semantic vector routing when a valid local model file
    is successfully loaded.
    """
    # Initially, model_path does not exist -> starts as mock
    model_dir = temp_dir / "my_model"
    analyzer = IncrementalAnalyzer(
        max_folders=3,
        stop_words={"the", "and"},
        db=db,
        strategy_name="default",
        model_path=str(model_dir),
    )

    assert analyzer.embedding_manager.is_mock is True

    # Set up some documents
    base_dir = str(temp_dir)
    db.clear(base_dir)

    hist_text = "space rockets Mars landing astronauts and cosmic deep space exploration mission"
    db.upsert_document(base_dir, "historical_space.txt", "hash_h", hist_text)
    db.set_user_verified_target(base_dir, "hash_h", "Space")

    # Under semantic matching we will also use highly similar documents to be robust
    new_text = (
        "space rockets Mars landing astronauts and cosmic deep space exploration flight"
    )
    corpus = {"new_space.txt": new_text}
    analyzer.partial_fit(base_dir, corpus)

    # Mock the transition: simulate a local model file successfully loading
    # We patch `get_active_model_properties` to return a valid profile representing a loaded model
    valid_properties = ModelProperties(
        signature="valid_mock_signature_abc123",
        dimensions=384,
        version="2.0.0",
        is_valid=True,
    )

    # We also mock `generate_embedding` to return a non-random, controllable vector
    # so we can verify semantic similarity executes
    dummy_vector_hist = [1.0] + [0.0] * 383
    dummy_vector_new = [1.0] + [0.0] * 383  # perfectly aligned, cosine_similarity = 1.0

    def mock_gen_embedding(text):
        return dummy_vector_hist

    with (
        patch(
            "app.core.semantic_embeddings.get_active_model_properties",
            return_value=valid_properties,
        ),
        patch.object(
            analyzer.embedding_manager,
            "generate_embedding",
            side_effect=mock_gen_embedding,
        ) as mock_gen,
    ):
        # Calling `is_mock` dynamically refreshes and detects that the engine is no longer in mock state
        assert analyzer.embedding_manager.is_mock is False
        assert analyzer.embedding_manager.is_model_valid is True
        assert analyzer.embedding_manager.signature == "valid_mock_signature_abc123"

        # We insert the hist vector in the DB *after* `is_mock` property checks so it doesn't get cleared by the purge
        db.upsert_document_vectors(
            base_dir, [("historical_space.txt", dummy_vector_hist)]
        )

        # Now, call generate_sorting_plan. Since use_semantic is True, it should generate embeddings
        plan = analyzer.generate_sorting_plan(base_dir)

        # 1. generate_embedding should have been called for new_space.txt to perform the matching
        mock_gen.assert_called()

        # 2. It successfully resolves standard matching task via semantic vectors
        assert "Space" in plan
        assert "new_space.txt" in plan["Space"]
        assert plan["Space"]["new_space.txt"]["routed_by"] == "similarity"
