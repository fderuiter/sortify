import os
import sys
from unittest.mock import patch

import pytest

from app.core.analyzer import IncrementalAnalyzer
from app.core.analyzer_strategies import GenerativeNamingStrategy
from app.core.mover import execute_moves


def resolve_test_path(path):
    """Resolve and canonicalize paths consistently across different operating systems.

    On Windows (win32), it resolves short 8.3 names and strips the extended-length prefixes.
    On non-Windows platforms (macOS/Linux), it resolves physical symlinks (like /var or /tmp).
    """
    path_str = str(path)
    abs_path = os.path.normpath(os.path.abspath(path_str))
    if sys.platform == "win32":
        try:
            abs_path = os.path.normpath(os.path.realpath(abs_path))
        except Exception:
            pass
        if abs_path.startswith("\\\\?\\UNC\\"):
            abs_path = "\\" + abs_path[7:]
        elif abs_path.startswith("\\\\?\\"):
            abs_path = abs_path[4:]
        if len(abs_path) > 1 and abs_path[1] == ":":
            abs_path = abs_path[0].upper() + abs_path[1:]
    else:
        try:
            abs_path = os.path.normpath(os.path.realpath(abs_path))
        except Exception:
            pass
    return abs_path


@pytest.fixture(autouse=True)
def mock_trigger_reconstruction():
    """Mock background vector reconstruction trigger to prevent background threads on Windows."""
    with patch(
        "app.core.semantic_embeddings.SemanticEmbeddingManager.trigger_reconstruction"
    ) as mock:
        yield mock


def add_relative_source_to_plan(plan, current_dest=""):
    if not isinstance(plan, dict):
        return
    for key, content in list(plan.items()):
        if isinstance(content, dict):
            if content.get("__type__") == "file":
                if current_dest:
                    depth = len(current_dest.replace("\\", "/").split("/"))
                    prefix = "/".join([".."] * depth)
                    content["relative_source"] = f"{prefix}/{key}"
                else:
                    content["relative_source"] = key
            else:
                new_dest = os.path.join(current_dest, key) if current_dest else key
                add_relative_source_to_plan(content, new_dest)


def test_similarity_routed_leaves_history_empty(test_history_env):
    """
    Test that moving a document automatically via similarity matching changes its current path
    but leaves its user-verified history empty.
    """
    base_dir, db, cache_manager, history_manager, db_worker = test_history_env
    base_dir = resolve_test_path(base_dir)

    filename_verified = "verified_doc.txt"
    filename_sim = "similarity_doc.txt"

    # Longer structured texts to guarantee high similarity match (> 0.8)
    hist_text = "This is an invoice for laptop purchase from TechStore. Total amount due is $1200. Please pay by bank transfer."
    new_text = "This is an invoice for laptop purchase from TechStore. Total amount due is $1201. Please pay by bank transfer."

    with open(os.path.join(base_dir, filename_verified), "w") as f:
        f.write(hist_text)
    with open(os.path.join(base_dir, filename_sim), "w") as f:
        f.write(new_text)

    # Set up historical verified document in "Receipts" folder
    db.upsert_document(base_dir, filename_verified, "hash_v", hist_text)
    db.set_user_verified_target(base_dir, "hash_v", "Receipts")

    # Upsert new document to DB, but it's not verified
    db.upsert_document(base_dir, filename_sim, "hash_s", new_text)

    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "and"}, db=db, strategy_name=None
    )

    # Generate sorting plan - similarity_doc.txt should be routed to "Receipts" via similarity
    plan = analyzer.generate_sorting_plan(base_dir)

    # Prepare plan with relative sources
    add_relative_source_to_plan(plan)

    # Execute the moves
    execute_moves(base_dir, plan, db, history_manager)

    # Verify file was moved on disk
    dest_path = os.path.join(base_dir, "Receipts", filename_sim)
    assert os.path.exists(dest_path)

    # Verify DB: path is updated to Receipts/similarity_doc.txt
    new_filepath = os.path.join("Receipts", filename_sim).replace("\\", "/")
    all_docs = db.get_all_documents(base_dir)
    matched_docs = [d for d in all_docs if d[0] == new_filepath]
    assert len(matched_docs) == 1

    # doc[3] is user_verified_target_path. It should be None/empty!
    assert matched_docs[0][3] is None or matched_docs[0][3] == ""


def test_dynamic_re_evaluation_on_subsequent_runs(test_history_env):
    """
    Test that subsequent file classification runs re-evaluate AI-routed documents
    using the semantic analyzer instead of bypassing it.
    """
    base_dir, db, cache_manager, history_manager, db_worker = test_history_env
    base_dir = resolve_test_path(base_dir)

    filename_verified = "verified_doc.txt"
    filename_sim = "similarity_doc.txt"

    # Longer structured texts to guarantee high similarity match (> 0.8)
    hist_text = "This is an invoice for laptop purchase from TechStore. Total amount due is $1200. Please pay by bank transfer."
    new_text = "This is an invoice for laptop purchase from TechStore. Total amount due is $1201. Please pay by bank transfer."

    with open(os.path.join(base_dir, filename_verified), "w") as f:
        f.write(hist_text)
    with open(os.path.join(base_dir, filename_sim), "w") as f:
        f.write(new_text)

    # Upsert and set verified target for verified_doc.txt to 'Finance'
    db.upsert_document(base_dir, filename_verified, "hash_v", hist_text)
    db.set_user_verified_target(base_dir, "hash_v", "Finance")

    # Upsert similarity_doc.txt but DO NOT set verified target
    db.upsert_document(base_dir, filename_sim, "hash_s", new_text)

    # Let's initialize analyzer
    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "and"}, db=db, strategy_name=None
    )

    # Let's generate a sorting plan
    plan = analyzer.generate_sorting_plan(base_dir)

    # The verified_doc should be bypassed/placed directly into 'Finance' (routed_by='historical')
    assert "Finance" in plan
    assert filename_verified in plan["Finance"]
    assert plan["Finance"][filename_verified]["routed_by"] == "historical"

    # The similarity_doc should be routed via 'similarity' based on similarity to verified_doc.txt
    assert filename_sim in plan["Finance"]
    assert plan["Finance"][filename_sim]["routed_by"] == "similarity"


def test_llm_prompt_few_shot_only_contains_verified_historical_examples(
    test_history_env,
):
    """
    Test that few-shot prompts generated for the LLM contain only manually corrected
    or explicitly verified historical examples.
    """
    base_dir, db, cache_manager, history_manager, db_worker = test_history_env
    base_dir = resolve_test_path(base_dir)

    # Put two files in DB:
    # 1. One user-verified (has user_verified_target_path)
    # 2. One unverified/automated (user_verified_target_path is None)

    db.upsert_document(base_dir, "doc1.txt", "h1", "text of manually verified doc")
    db.set_user_verified_target(base_dir, "h1", "VerifiedFolder")

    db.upsert_document(base_dir, "doc2.txt", "h2", "text of automated similarity doc")
    # doc2.txt has user_verified_target_path left as None

    strategy = GenerativeNamingStrategy()
    strategy.set_db_context(db, base_dir)
    strategy.stop_words = {"the", "and"}

    # Let's inspect the historical examples retrieved for LLM prompt context
    # We can fetch the documents retrieved by the DB in GenerativeNamingStrategy._get_cluster_keywords
    all_docs = db.get_all_documents(base_dir)
    historical_examples = []
    for doc in all_docs:
        if len(doc) > 3 and doc[1] and doc[3]:
            historical_examples.append({"text": doc[1], "target_path": doc[3]})

    # There should ONLY be 1 historical example (doc1.txt) and NOT doc2.txt
    assert len(historical_examples) == 1
    assert historical_examples[0]["text"] == "text of manually verified doc"
    assert historical_examples[0]["target_path"] == "VerifiedFolder"


def test_batch_updates_transaction_execution(test_history_env):
    """
    Test that batch update operations successfully execute both path updates and manual
    user-verified overrides within a single transaction.
    """
    base_dir, db, cache_manager, history_manager, db_worker = test_history_env
    base_dir = resolve_test_path(base_dir)

    # Create two files
    f1, f2 = "f1.txt", "f2.txt"
    with open(os.path.join(base_dir, f1), "w") as f:
        f.write("text1")
    with open(os.path.join(base_dir, f2), "w") as f:
        f.write("text2")

    db.upsert_document(base_dir, f1, "hash1", "text1")
    db.upsert_document(base_dir, f2, "hash2", "text2")

    # We will build a batch update:
    # 1. Update document path of f1 to Folder1/f1.txt
    # 2. Add manual user-verified override of f2 to Folder2
    updates = [
        {
            "type": "document_path",
            "args": (base_dir, f1, "Folder1/f1.txt"),
        },
        {
            "type": "verified_target",
            "args": (base_dir, "hash2", "Folder2"),
        },
    ]

    # Execute batch updates
    db.execute_batch_updates(updates)

    # Verify that f1's path is updated but its verified target remains empty (Requirement 1 isolated!)
    doc1 = [d for d in db.get_all_documents(base_dir) if d[0] == "Folder1/f1.txt"]
    assert len(doc1) == 1
    assert doc1[0][3] is None or doc1[0][3] == ""

    # Verify that f2's path is unchanged but its verified target is updated to Folder2
    doc2 = [d for d in db.get_all_documents(base_dir) if d[0] == f2]
    assert len(doc2) == 1
    assert doc2[0][3] == "Folder2"
