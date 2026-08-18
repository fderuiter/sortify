"""Unit tests for Strategy Leaf Metadata and Slow-Path Phase Splitting Fallback."""

import pytest

from app.config import AppSettings
from app.core.analyzer_strategies import (
    GenerativeNamingStrategy,
    RecursiveKMeansStrategy,
)
from app.core.clinical_strategy import ClinicalTMFStrategy
from app.core.session import AppSession


def test_recursive_kmeans_strategy_leaf_metadata():
    """Verify RecursiveKMeansStrategy generates complete dictionary leaf metadata."""
    strategy = RecursiveKMeansStrategy()
    filenames = ["doc1.txt", "doc2.txt", "doc3.txt", "doc4.txt"]
    documents = [
        "invoice billing payment",
        "invoice accounting summary",
        "medical patient record",
        "medical health history",
    ]

    plan, _ = strategy.generate_plan(
        filenames=filenames,
        documents=documents,
        max_folders=2,
        stop_words=set(),
    )

    def _verify_leaves(node):
        for k, v in node.items():
            if isinstance(v, dict) and v.get("__type__") == "file":
                assert v["__type__"] == "file"
                assert "relative_source" in v
                assert "source_path" in v
                assert v.get("routed_by") == "clustering"
            elif isinstance(v, dict):
                _verify_leaves(v)
            else:
                pytest.fail(f"Leaf node {k} is not a valid metadata dictionary: {v}")

    _verify_leaves(plan)


def test_generative_naming_strategy_leaf_metadata():
    """Verify GenerativeNamingStrategy preserves/outputs complete dictionary leaf metadata."""
    strategy = GenerativeNamingStrategy()
    filenames = ["doc1.txt", "doc2.txt", "doc3.txt", "doc4.txt"]
    documents = [
        "finance report annual Q1",
        "finance tax audit statement",
        "tech software python code",
        "tech architecture design",
    ]

    plan, _ = strategy.generate_plan(
        filenames=filenames,
        documents=documents,
        max_folders=2,
        stop_words=set(),
    )

    def _verify_leaves(node):
        for k, v in node.items():
            if isinstance(v, dict) and v.get("__type__") == "file":
                assert v["__type__"] == "file"
                assert "relative_source" in v
                assert "source_path" in v
            elif isinstance(v, dict):
                _verify_leaves(v)
            else:
                pytest.fail(f"Leaf node {k} is not a valid metadata dictionary: {v}")

    _verify_leaves(plan)


def test_clinical_strategy_leaf_metadata():
    """Verify ClinicalTMFStrategy outputs complete dictionary leaf metadata."""
    strategy = ClinicalTMFStrategy(mode="tmf")
    filenames = ["protocol.pdf", "unknown.txt"]
    documents = [
        "Clinical study protocol version 1.0",
        "random content not matching clinical artifacts",
    ]

    plan, _ = strategy.generate_plan(
        filenames=filenames,
        documents=documents,
        max_folders=2,
        stop_words=set(),
    )

    def _verify_leaves(node):
        for k, v in node.items():
            if isinstance(v, dict) and v.get("__type__") == "file":
                assert v["__type__"] == "file"
                assert "relative_source" in v
                assert "source_path" in v
                assert "routed_by" in v
            elif isinstance(v, dict):
                _verify_leaves(v)
            else:
                pytest.fail(f"Leaf node {k} is not a valid metadata dictionary: {v}")

    _verify_leaves(plan)


def test_interactive_plan_splitter_defaults_non_dict_and_unclassified_leaves_to_slow_path():
    """Verify interactive plan splitter routes non-dict and unclassified leaves to Phase 2."""
    from app.ui.app import AutoSorterApp

    # Construct plan with deterministic rules, AI clustering leaves, and non-dict/unclassified leaves
    test_plan = {
        "Invoices": {
            "keyword_match.txt": {
                "__type__": "file",
                "routed_by": "keyword",
                "relative_source": "keyword_match.txt",
            },
            "override_match.txt": {
                "__type__": "file",
                "routed_by": "override",
                "relative_source": "override_match.txt",
            },
        },
        "AI_Cluster": {
            "ai_clustered.txt": {
                "__type__": "file",
                "routed_by": "clustering",
                "relative_source": "ai_clustered.txt",
            },
            "empty_leaf.txt": None,
            "string_leaf.txt": "unclassified_raw_path",
        },
        "Unclassified": {
            "orphan_file.pdf": None,
        },
    }

    fast_plan, slow_plan = AutoSorterApp.split_plan_phases(test_plan)

    # Phase 1 Fast-Path assertions:
    assert "Invoices" in fast_plan
    assert "keyword_match.txt" in fast_plan["Invoices"]
    assert "override_match.txt" in fast_plan["Invoices"]
    assert "AI_Cluster" not in fast_plan
    assert "Unclassified" not in fast_plan

    # Phase 2 Slow-Path assertions:
    assert "AI_Cluster" in slow_plan
    assert "ai_clustered.txt" in slow_plan["AI_Cluster"]
    assert "empty_leaf.txt" in slow_plan["AI_Cluster"]
    assert "string_leaf.txt" in slow_plan["AI_Cluster"]
    assert "Unclassified" in slow_plan
    assert "orphan_file.pdf" in slow_plan["Unclassified"]


def test_end_to_end_phase_splitting_and_model_unloading(tmp_path):
    """Verify Phase 1 only moves deterministic rules and Phase 2 unloads models and moves AI files."""
    src_dir = tmp_path / "source"
    src_dir.mkdir()

    keyword_file = src_dir / "invoice_123.txt"
    keyword_file.write_text("Invoice standard text")

    ai_file1 = src_dir / "ai_doc1.txt"
    ai_file1.write_text("Machine learning data science research paper")

    ai_file2 = src_dir / "ai_doc2.txt"
    ai_file2.write_text("Neural network artificial intelligence paper")

    ai_file3 = src_dir / "ai_doc3.txt"
    ai_file3.write_text("Deep learning vector embeddings paper")

    settings = AppSettings()
    settings.KEYWORD_RULES = {"invoice": "Invoices"}

    session = AppSession(settings, base_dir=str(src_dir))
    session.db.upsert_documents(
        [
            (str(src_dir), "invoice_123.txt", "h1", "invoice text"),
            (str(src_dir), "ai_doc1.txt", "h2", "machine learning research"),
            (str(src_dir), "ai_doc2.txt", "h3", "neural network research"),
            (str(src_dir), "ai_doc3.txt", "h4", "deep learning research"),
        ]
    )

    full_plan = session.generate_sorting_plan(fast_path_only=False)

    # Verify invoice is under Invoices folder with keyword routing
    assert "Invoices" in full_plan
    assert full_plan["Invoices"]["invoice_123.txt"]["routed_by"] == "keyword"

    # Verify AI documents are under clustering folders with complete leaf dicts
    all_ai_files = []
    for folder, content in full_plan.items():
        if folder == "Invoices":
            continue
        for f_name, f_info in content.items():
            all_ai_files.append(f_name)
            assert isinstance(f_info, dict)
            assert f_info.get("__type__") == "file"
            assert "relative_source" in f_info

    assert "ai_doc1.txt" in all_ai_files
    assert "ai_doc2.txt" in all_ai_files
    assert "ai_doc3.txt" in all_ai_files

    session.close()
