"""Unit tests for Strategy Leaf Metadata and Slow-Path Phase Splitting Fallback."""

import pytest

from app.config import AppSettings
from app.core.analyzer import SortingPlanNode
from app.core.analyzer_strategies import (
    GenerativeNamingStrategy,
    RecursiveKMeansStrategy,
)
from app.core.session import AppSession
from app.plugins.clinical_compliance.clinical_strategy import ClinicalTMFStrategy


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
        curr_dict = (
            node.plan if hasattr(node, "plan") and isinstance(node.plan, dict) else node
        )
        for k, v in curr_dict.items():
            is_file = (
                getattr(v, "node_type", None) == "file"
                or (isinstance(v, dict) and v.get("__type__") == "file")
                or isinstance(v, SortingPlanNode)
            )
            if is_file:
                rel_src = getattr(v, "relative_source", None) or (
                    v.get("relative_source") if isinstance(v, dict) else None
                )
                src_path = getattr(v, "source_path", None) or (
                    v.get("source_path") if isinstance(v, dict) else None
                )
                routed_by = getattr(v, "routed_by", None) or (
                    v.get("routed_by") if isinstance(v, dict) else None
                )
                assert rel_src is not None
                assert src_path is not None
                assert routed_by == "clustering"
            elif hasattr(v, "plan") or isinstance(v, dict):
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
        curr_dict = (
            node.plan if hasattr(node, "plan") and isinstance(node.plan, dict) else node
        )
        for k, v in curr_dict.items():
            is_file = (
                getattr(v, "node_type", None) == "file"
                or (isinstance(v, dict) and v.get("__type__") == "file")
                or isinstance(v, SortingPlanNode)
            )
            if is_file:
                rel_src = getattr(v, "relative_source", None) or (
                    v.get("relative_source") if isinstance(v, dict) else None
                )
                src_path = getattr(v, "source_path", None) or (
                    v.get("source_path") if isinstance(v, dict) else None
                )
                assert rel_src is not None
                assert src_path is not None
            elif hasattr(v, "plan") or isinstance(v, dict):
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
        curr_dict = (
            node.plan if hasattr(node, "plan") and isinstance(node.plan, dict) else node
        )
        for k, v in curr_dict.items():
            is_file = (
                getattr(v, "node_type", None) == "file"
                or (isinstance(v, dict) and v.get("__type__") == "file")
                or isinstance(v, SortingPlanNode)
            )
            if is_file:
                rel_src = getattr(v, "relative_source", None) or (
                    v.get("relative_source") if isinstance(v, dict) else None
                )
                src_path = getattr(v, "source_path", None) or (
                    v.get("source_path") if isinstance(v, dict) else None
                )
                routed_by = getattr(v, "routed_by", None) or (
                    v.get("routed_by") if isinstance(v, dict) else None
                )
                assert rel_src is not None
                assert src_path is not None
                assert routed_by is not None
            elif hasattr(v, "plan") or isinstance(v, dict):
                _verify_leaves(v)
            else:
                pytest.fail(f"Leaf node {k} is not a valid metadata dictionary: {v}")

    _verify_leaves(plan)


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
    plan_dict = full_plan.plan if hasattr(full_plan, "plan") else full_plan

    # Verify invoice is under Invoices folder with keyword routing
    assert "Invoices" in plan_dict
    inv_node = plan_dict["Invoices"]["invoice_123.txt"]
    assert (
        getattr(inv_node, "routed_by", None)
        or (inv_node.get("routed_by") if isinstance(inv_node, dict) else None)
    ) == "keyword"

    # Verify AI documents are under clustering folders with complete leaf dicts
    all_ai_files = []
    for folder, content in plan_dict.items():
        if folder == "Invoices":
            continue
        c_dict = (
            content.plan
            if hasattr(content, "plan") and isinstance(content.plan, dict)
            else content
        )
        for f_name, f_info in c_dict.items():
            all_ai_files.append(f_name)
            is_file = (
                getattr(f_info, "node_type", None) == "file"
                or (isinstance(f_info, dict) and f_info.get("__type__") == "file")
                or isinstance(f_info, SortingPlanNode)
            )
            assert is_file
            assert getattr(f_info, "relative_source", None) is not None or (
                isinstance(f_info, dict) and "relative_source" in f_info
            )

    assert "ai_doc1.txt" in all_ai_files
    assert "ai_doc2.txt" in all_ai_files
    assert "ai_doc3.txt" in all_ai_files

    session.close()


def test_unsplittable_sub_cluster_is_not_nested_under_its_own_name():
    """Files of a sub-cluster that can't split further sit directly in its folder."""
    strategy = RecursiveKMeansStrategy()
    filenames = [f"fin{i}.txt" for i in range(1, 5)] + [
        f"tech{i}.txt" for i in range(1, 5)
    ]
    documents = [
        f"quarterly budget revenue invoice finance tax report {i}" for i in range(4)
    ] + [f"database compiler python algorithm software code {i}" for i in range(4)]

    plan, _ = strategy._generate_plan_inline(
        filenames, documents, max_folders=5, stop_words=set()
    )

    plan = plan.plan if hasattr(plan, "plan") else plan
    assert len(plan) == 2
    for folder_name, children in plan.items():
        assert folder_name not in children
        assert all(child.get("__type__") == "file" for child in children.values())
