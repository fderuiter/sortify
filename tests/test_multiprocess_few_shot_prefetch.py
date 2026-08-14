import os
import shutil
import tempfile
import time
from pathlib import Path
import pytest

from app.core.analyzer import IncrementalAnalyzer
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.semantic_embeddings import ModelProperties

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

def test_multiprocess_few_shot_prefetch_flow(db, temp_dir):
    """
    Verify:
    1. Parent process queries, decrypts, and packages historical examples.
    2. Background child process receives these examples in memory.
    3. Examples are successfully injected into naming prompts without any child-process DB access.
    """
    base_dir = str(temp_dir)
    db.clear(base_dir)

    # Set up active model metadata in the database to prevent verify_active_model from purging the database
    db.set_model_metadata("active_model_signature", "mock_sig_hash_prefetch")
    db.set_model_metadata("active_model_dimensions", "384")
    db.set_model_metadata("active_model_version", "1.0.0")

    # Insert historical documents
    db.upsert_document(
        base_dir,
        "hist1.txt",
        "hash_h1",
        "Mars rocket space exploration NASA",
    )
    db.set_user_verified_target(base_dir, "hash_h1", "Space")

    db.upsert_document(
        base_dir,
        "hist2.txt",
        "hash_h2",
        "chef kitchen cooking recipe food",
    )
    db.set_user_verified_target(base_dir, "hash_h2", "Cooking")

    # Insert corresponding vectors (384 dimensions)
    vector_space = [1.0] + [0.0] * 383
    vector_cooking = [0.0, 1.0] + [0.0] * 382

    db.upsert_document_vectors(
        base_dir,
        [
            ("hist1.txt", vector_space),
            ("hist2.txt", vector_cooking),
        ],
    )

    # Insert target documents into database so generate_sorting_plan can retrieve them
    db.upsert_document(
        base_dir,
        "doc1.txt",
        "hash_d1",
        "astronomy telescopes stars galaxies space",
    )
    db.upsert_document(
        base_dir,
        "doc2.txt",
        "hash_d2",
        "pizza restaurant cheese dough food",
    )
    db.upsert_document(
        base_dir,
        "doc3.txt",
        "hash_d3",
        "quantum physics particle mechanics relativity physics",
    )
    # Insert corresponding vectors for target documents (orthogonal to avoid parent routing)
    db.upsert_document_vectors(
        base_dir,
        [
            ("doc1.txt", [0.0, 0.0, 1.0] + [0.0] * 381),
            ("doc2.txt", [0.0, 0.0, 0.0, 1.0] + [0.0] * 380),
            ("doc3.txt", [0.0, 0.0, 0.0, 0.0, 1.0] + [0.0] * 379),
        ],
    )

    # Set up prompt dump file path
    prompt_dump_path = temp_dir / "prompt_dump.txt"
    os.environ["PROMPT_DUMP_FILE"] = str(prompt_dump_path)
    os.environ["FORCE_MULTIPROCESSING_CLUSTERING"] = "1"

    # Instantiate Analyzer which runs clustering and generative folder naming
    analyzer = IncrementalAnalyzer(
        max_folders=2,
        stop_words={"the", "and"},
        db=db,
        strategy_name="generative",
        model_path=str(temp_dir), # point to a valid folder to pass checks
    )

    # We must patch the active model properties to match
    from unittest.mock import patch
    valid_properties = ModelProperties(
        signature="mock_sig_hash_prefetch",
        dimensions=384,
        version="1.0.0",
        is_valid=True,
    )

    with (
        patch(
            "app.core.semantic_embeddings.get_active_model_properties",
            return_value=valid_properties,
        ),
    ):
        # Trigger generate_sorting_plan which runs child-process clustering and naming
        plan = analyzer.generate_sorting_plan(
            base_dir=base_dir,
        )

        assert plan is not None

    # Wait briefly to ensure file writes finish
    time.sleep(0.5)

    # Clean up environment variables
    os.environ.pop("PROMPT_DUMP_FILE", None)
    os.environ.pop("FORCE_MULTIPROCESSING_CLUSTERING", None)

    # Read the dumped prompts
    assert prompt_dump_path.exists(), "Prompt dump file was not created by the child process"
    with open(prompt_dump_path, "r") as f:
        prompts_text = f.read()

    # Verify that the pre-fetched historical examples are successfully injected into the prompts!
    assert "Mars rocket space exploration NASA" in prompts_text
    assert "Space" in prompts_text
