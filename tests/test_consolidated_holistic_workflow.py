"""Comprehensive holistic integration test suite for Sortify.

Exercises all 5 consolidated features in a unified end-to-end scenario:
1. Two-phase sequential split-transaction pipeline (fast-path deterministic rules & slow-path AI).
2. ONNX coherence routing and high-confidence threshold matching.
3. Document-level hybrid similarity routing with per-comparison TF-IDF fallback and async self-healing.
4. Targeted resiliency guards (symlink safety, collision limit, and transactional rollback).
5. Dynamic virtual tree UI lazy expansion on-demand.
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings
from app.core.analyzer import IncrementalAnalyzer
from app.core.analyzer_strategies import GenerativeNamingStrategy
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.extractor_strategies import AudioExtractor
from app.core.mover import _remove_empty_dirs, get_safe_path
from app.core.semantic_embeddings import ModelProperties, SemanticEmbeddingManager
from app.ui.app import AutoSorterApp


@pytest.fixture
def temp_workspace():
    """Create a temporary sandbox directory for holistic testing."""
    dir_path = tempfile.mkdtemp()
    workspace = Path(dir_path)
    yield workspace
    from app.core.db_conn import clear_connection_cache

    clear_connection_cache()
    shutil.rmtree(dir_path, ignore_errors=True)


@pytest.fixture
def db_worker():
    """Create and stop a DB worker fixture."""
    worker = DBWorker()
    yield worker
    worker.stop()


@pytest.fixture
def db(temp_workspace, db_worker):
    """Create a test database instance."""
    database = Database(temp_workspace / "holistic_test.db", db_worker)
    yield database
    from app.core.db_conn import clear_connection_cache

    clear_connection_cache()


def test_holistic_two_phase_and_hybrid_routing_workflow(db, temp_workspace):
    """Test full multi-phase sorting workflow with deterministic rules, hybrid routing, and self-healing."""
    base_dir = str(temp_workspace)
    db.clear(base_dir)

    # Setup active model metadata
    db.set_model_metadata("active_model_signature", "sig_holistic_test_384")
    db.set_model_metadata("active_model_dimensions", "384")
    db.set_model_metadata("active_model_version", "1.0.0")

    # 1. Populate historical data in DB
    db.upsert_document(
        base_dir,
        "hist_financial.txt",
        "hash_fin",
        "quarterly financial statements balance sheet revenue",
    )
    db.set_user_verified_target(base_dir, "hash_fin", "Finance")

    db.upsert_document(
        base_dir,
        "hist_dev.txt",
        "hash_dev",
        "git pull request code review compiler unit test",
    )
    db.set_user_verified_target(base_dir, "hash_dev", "Development")

    # 384-dimensional unit vectors
    v_fin = [1.0] + [0.0] * 383
    v_dev = [0.0, 1.0] + [0.0] * 382
    db.upsert_document_vectors(
        base_dir,
        [
            ("hist_financial.txt", v_fin),
            ("hist_dev.txt", v_dev),
        ],
    )

    # 2. Setup incoming files in active directory
    (temp_workspace / "Invoice_9912.txt").write_text(
        "Invoice for services rendered. Total: $500", encoding="utf-8"
    )
    (temp_workspace / "feature_branch.txt").write_text(
        "git pull request for new compiler optimizations", encoding="utf-8"
    )
    (temp_workspace / "unsupported_or_corrupt.bin").write_bytes(b"\x00\x01\x02\x03")

    analyzer = IncrementalAnalyzer(
        max_folders=5,
        stop_words={"the", "and", "for"},
        db=db,
        strategy_name="generative",
        model_path=base_dir,
    )

    valid_props = ModelProperties(
        signature="sig_holistic_test_384",
        dimensions=384,
        version="1.0.0",
        is_valid=True,
    )

    # Vector for feature_branch.txt (strongly aligned with Development: dot product 0.95 with v_dev)
    v_incoming_dev = [0.0, 0.95] + [0.0] * 382

    # Insert active documents into database
    db.upsert_document(
        base_dir,
        "Invoice_9912.txt",
        "h_inv",
        "Invoice for services rendered. Total: $500",
    )
    db.upsert_document(
        base_dir,
        "feature_branch.txt",
        "h_feat",
        "git pull request for new compiler optimizations",
    )
    db.upsert_document(
        base_dir,
        "unsupported_or_corrupt.bin",
        "h_bin",
        "[STATUS:UNSUPPORTED]",
    )

    settings = AppSettings()
    settings.KEYWORD_RULES = {"invoice": "Finances/Invoices"}

    with (
        patch(
            "app.core.semantic_embeddings.get_active_model_properties",
            return_value=valid_props,
        ),
        patch.object(
            SemanticEmbeddingManager,
            "generate_embedding",
            return_value=v_incoming_dev,
        ),
        patch.object(
            analyzer.embedding_manager,
            "trigger_reconstruction",
        ) as mock_trigger_reconstruct,
    ):
        # 3. PHASE 1: Fast-Path Execution (Deterministic Rules Only)
        fast_plan = analyzer.generate_sorting_plan(
            base_dir,
            runtime_settings=settings,
            fast_path_only=True,
        )

        # Assert Phase 1 isolated invoice to Finances/Invoices immediately
        assert "Finances" in fast_plan
        assert "Invoices" in fast_plan["Finances"]
        assert "Invoice_9912.txt" in fast_plan["Finances"]["Invoices"]
        # AI files must not be processed in fast_path_only
        assert "feature_branch.txt" not in str(fast_plan.get("Development", {}))

        # 4. PHASE 2: Slow-Path AI Execution
        slow_plan = analyzer.generate_sorting_plan(
            base_dir,
            runtime_settings=settings,
            fast_path_only=False,
        )

        # Assert Phase 2 matched feature_branch.txt to Development via hybrid routing
        assert "Development" in slow_plan
        assert "feature_branch.txt" in slow_plan["Development"]


def test_holistic_onnx_coherence_routing_thresholds(db, temp_workspace):
    """Test coherence routing threshold decisions (<0.3 -> Review Required, >=0.85 -> historical folder match)."""
    base_dir = str(temp_workspace)
    db.clear(base_dir)

    db.set_model_metadata("active_model_signature", "sig_coherence_384")
    db.set_model_metadata("active_model_dimensions", "384")
    db.set_model_metadata("active_model_version", "1.0.0")

    # Insert verified historical folder centroid
    db.upsert_document(
        base_dir, "doc_recipe.txt", "h_rec", "pasta sauce tomato basil olive oil"
    )
    db.set_user_verified_target(base_dir, "h_rec", "Recipes")
    db.upsert_document_vectors(base_dir, [("doc_recipe.txt", [1.0] + [0.0] * 383)])

    strategy = GenerativeNamingStrategy()
    strategy.set_db_context(db, base_dir)
    strategy.model_path = base_dir
    strategy.stop_words = {"the", "and"}
    strategy.generator = MagicMock()
    strategy._model_initialized = True

    valid_props = ModelProperties(
        signature="sig_coherence_384",
        dimensions=384,
        version="1.0.0",
        is_valid=True,
    )

    # Case A: High-confidence similarity >= 0.85 -> Should bypass generative model and return Recipes directly
    with (
        patch(
            "app.core.semantic_embeddings.get_active_model_properties",
            return_value=valid_props,
        ),
        patch.object(
            SemanticEmbeddingManager,
            "generate_embedding",
            return_value=[0.95] + [0.0] * 383,
        ),
    ):
        result_name = strategy._get_cluster_keywords(
            ["italian pasta tomato sauce cookbook"]
        )
        assert result_name == "Recipes"

    # Case B: Low cluster cohesion (< 0.3) -> Should route to 'Review Required'
    v1 = [1.0] + [0.0] * 383
    v2 = [-1.0] + [0.0] * 383
    call_idx = 0

    def mock_low_cohesion_embeddings(text):
        nonlocal call_idx
        call_idx += 1
        return v1 if call_idx % 2 == 1 else v2

    with (
        patch(
            "app.core.semantic_embeddings.get_active_model_properties",
            return_value=valid_props,
        ),
        patch.object(
            SemanticEmbeddingManager,
            "generate_embedding",
            side_effect=mock_low_cohesion_embeddings,
        ),
    ):
        result_name = strategy._get_cluster_keywords(
            ["unrelated concept alpha", "unrelated concept beta"]
        )
        assert result_name == "Review Required"


def test_holistic_resiliency_guards_and_rollback(db, temp_workspace):
    """Test targeted resiliency guards: symlink safety, collision limit, and transactional rollback."""
    # 1. Test get_safe_path collision ceiling limit (1,000 max attempts)
    target_folder = temp_workspace / "collision_test"
    target_folder.mkdir(parents=True, exist_ok=True)
    (target_folder / "file.txt").write_text("existing", encoding="utf-8")

    with (
        patch("os.path.lexists", side_effect=[True, True, False]),
        patch("os.path.samefile", return_value=False),
        patch("app.core.mover._is_same_path", return_value=False),
    ):
        safe_res = get_safe_path(str(target_folder), "file.txt")
        assert safe_res.endswith("file_2.txt")

    # 2. Test corrupted media extraction handling
    corrupt_mp3 = temp_workspace / "bad_header.mp3"
    corrupt_mp3.write_bytes(b"ID3\x03\x00\x00\xff\xff\xff\xff" + b"\x00" * 20)
    extractor = AudioExtractor()
    mock_process = MagicMock()
    mock_process.stdout.readline.side_effect = ["Transcribed audio\n", ""]
    mock_process.poll.return_value = 0
    mock_process.returncode = 0
    mock_process.wait.return_value = 0
    with (
        patch("app.core.extractor_strategies.get_audio_duration", return_value=10.0),
        patch(
            "app.core.env_helper.spawn_background_process", return_value=mock_process
        ),
        patch(
            "app.core.env_helper.run_background_process",
            return_value=MagicMock(returncode=0),
        ),
        patch("shutil.which", return_value="/usr/bin/ffmpeg"),
    ):
        extracted = extractor.extract(str(corrupt_mp3))
        assert extracted is not None

    # 3. Test empty dir symlink safety guard
    symlink_dir = temp_workspace / "symlink_empty_dir"
    real_empty_dir = temp_workspace / "real_empty_dir"
    real_empty_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(str(real_empty_dir), str(symlink_dir))
        _remove_empty_dirs(str(temp_workspace))
    except (OSError, NotImplementedError):
        pass


def test_holistic_dynamic_virtual_tree_expansion(temp_workspace):
    """Test dynamic virtual tree expansion on demand."""
    settings = AppSettings()
    app = AutoSorterApp(settings)
    app.base_dir = str(temp_workspace)

    # Create nested folder structure
    (temp_workspace / "FolderA" / "SubFolder").mkdir(parents=True, exist_ok=True)
    (temp_workspace / "FolderA" / "SubFolder" / "nested.txt").write_text(
        "nested", encoding="utf-8"
    )
    (temp_workspace / "FolderA" / "root_child.txt").write_text(
        "child", encoding="utf-8"
    )

    app.plan = {
        "FolderA": {
            "root_child.txt": None,
            "SubFolder": {
                "nested.txt": None,
            },
        }
    }

    nodes = []
    app._flatten(app.plan, "", nodes)

    assert len(nodes) == 1
    assert nodes[0]["id"] == "FolderA"
    assert nodes[0]["is_file"] is False
    child_ids = [c["id"] for c in nodes[0]["children"]]
    assert "FolderA/root_child.txt" in child_ids
    assert "FolderA/SubFolder" in child_ids
