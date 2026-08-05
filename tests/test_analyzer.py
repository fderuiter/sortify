import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.analyzer import IncrementalAnalyzer
from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager

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
    history_manager = HistoryManager(
        db, cache_manager, str(Path(_test_dir) / "history.db")
    )


def teardown_module(module):
    global _test_dir, db_worker
    if db_worker:
        db_worker.stop()
    import shutil

    if _test_dir:
        shutil.rmtree(_test_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_db():
    db.clear()
    yield


def test_incremental_analyzer_init():
    analyzer = IncrementalAnalyzer(
        max_folders=5, stop_words={"the", "and"}, db=db, model_path="all-MiniLM-L6-v2"
    )
    assert analyzer.max_folders == 5
    assert analyzer.corpus == {}


def test_partial_fit():
    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "and"}, db=db, model_path="all-MiniLM-L6-v2"
    )
    corpus = {
        "file1.txt": "This is a document about finance and money.",
        "file2.txt": "Science and technology are great.",
    }

    analyzer.partial_fit("dummy_base", corpus)
    assert len(analyzer.corpus) == 2
    assert "file1.txt" in analyzer.corpus


def test_partial_fit_empty():
    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "and"}, db=db, model_path="all-MiniLM-L6-v2"
    )
    analyzer.partial_fit("dummy_base", {})
    assert len(analyzer.corpus) == 0


def test_generate_sorting_plan_empty():
    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "and"}, db=db, model_path="all-MiniLM-L6-v2"
    )
    plan = analyzer.generate_sorting_plan("dummy_base")
    assert plan == {}


def test_generate_sorting_plan():
    analyzer = IncrementalAnalyzer(
        max_folders=2, stop_words={"the", "and"}, db=db, model_path="all-MiniLM-L6-v2"
    )
    corpus = {
        "finance1.txt": "money bank finance investment",
        "finance2.txt": "investment stock market money",
        "tech1.txt": "software computer science technology",
        "tech2.txt": "technology hardware computer",
    }
    analyzer.partial_fit("dummy_base", corpus)
    plan = analyzer.generate_sorting_plan("dummy_base")

    # Check that there are at least some folders created or files added
    assert isinstance(plan, dict)
    assert len(plan) > 0


def test_partial_fit_exception(mocker):
    analyzer = IncrementalAnalyzer(
        max_folders=2, stop_words={"the", "and"}, db=db, model_path="all-MiniLM-L6-v2"
    )
    mocker.patch.object(db, "upsert_documents", side_effect=Exception("Test error"))
    mock_logger = mocker.patch("app.core.analyzer.logging.error")

    corpus = {"unique_file_exception.txt": "unique test content exception"}
    analyzer.partial_fit("dummy_base", corpus)

    mock_logger.assert_called_once()
    assert (
        "unique_file_exception.txt" in analyzer.corpus
    )  # Update still happened before exception


def test_generate_sorting_plan_exception(mocker):
    analyzer = IncrementalAnalyzer(
        max_folders=2, stop_words={"the", "and"}, db=db, model_path="all-MiniLM-L6-v2"
    )
    corpus = {"file.txt": "test content"}
    analyzer.partial_fit("dummy_base", corpus)

    mocker.patch.object(db, "get_all_documents_lazy", side_effect=Exception("Test error"))
    mock_logger = mocker.patch("app.core.analyzer.logging.error")

    plan = analyzer.generate_sorting_plan("dummy_base")

    mock_logger.assert_called_once()
    assert plan == {}


def test_naming_collision_resolution():
    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the"}, db=db, model_path="all-MiniLM-L6-v2"
    )
    # We want two topics to have the same primary keywords, but different term frequencies
    corpus = {
        "file1.txt": "apple banana apple banana apple orange",
        "file2.txt": "apple banana apple banana grape grape grape grape",
        "file3.txt": "apple banana apple banana peach peach",
        "file4.txt": "apple banana apple banana kiwi kiwi kiwi kiwi kiwi",
    }
    analyzer.partial_fit("dummy_base", corpus)
    plan = analyzer.generate_sorting_plan("dummy_base")

    folder_names = list(plan.keys())
    assert "Miscellaneous" not in folder_names or len(folder_names) > 1


def test_conflict_detection():
    db.clear("test_conflict_base")
    # File matches both a keyword rule and has a historical override
    # keyword rule: "invoice" -> "Accounting"
    # historical override: "Archive"

    analyzer = IncrementalAnalyzer(max_folders=2, stop_words={"the", "and"}, db=db)
    corpus = {"invoice_2025.txt": "Some invoice text"}

    # Put document in DB with an assigned folder (historical override)
    db.upsert_document(
        "test_conflict_base", "invoice_2025.txt", "hash123", "Some invoice text"
    )
    db.set_user_verified_target("test_conflict_base", "hash123", "Archive")

    analyzer.partial_fit("test_conflict_base", corpus)

    settings = SimpleNamespace(KEYWORD_RULES={"invoice": "Accounting"})

    plan = analyzer.generate_sorting_plan("test_conflict_base", settings)

    # invoice_2025.txt should be in the plan under 'Archive' and flagged as conflicted
    assert "Archive" in plan
    file_info = plan["Archive"]["invoice_2025.txt"]
    assert file_info.get("is_conflicted") is True
    assert file_info.get("compliance_path") == "Accounting"
    assert file_info.get("historical_path") == "Archive"


def test_conflict_resolution():
    db.clear("test_conflict_res_base")

    analyzer = IncrementalAnalyzer(max_folders=2, stop_words={"the", "and"}, db=db)
    corpus = {"invoice_2025.txt": "Some invoice text"}

    db.upsert_document(
        "test_conflict_res_base", "invoice_2025.txt", "hash123", "Some invoice text"
    )
    db.set_user_verified_target("test_conflict_res_base", "hash123", "Archive")

    analyzer.partial_fit("test_conflict_res_base", corpus)

    # Pre-populate session cache with a locked choice
    locked_files = {"invoice_2025.txt": "Accounting"}  # user chose compliance path

    settings = SimpleNamespace(KEYWORD_RULES={"invoice": "Accounting"})

    plan = analyzer.generate_sorting_plan(
        "test_conflict_res_base", settings, locked_files
    )

    # Since it was resolved to 'Accounting', it should be in Accounting and no longer flagged as conflicted
    assert "Accounting" in plan
    file_info = plan["Accounting"]["invoice_2025.txt"]
    assert file_info.get("is_conflicted", False) is False


def test_document_to_document_similarity_matching():
    # Setup base directory
    base_dir = "test_similarity_matching_base"
    db.clear(base_dir)

    # Initialize analyzer with strategy_name=None to bypass heavy clustering
    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "and"}, db=db, strategy_name=None
    )

    # Historical document (manually verified/sorted to "Receipts")
    # Using more structured text to ensure strong cosine similarity
    hist_text = "This is an invoice for laptop purchase from TechStore. Total amount due is $1200. Please pay by bank transfer."
    db.upsert_document(base_dir, "historical_receipt.txt", "hash_hist_1", hist_text)
    db.set_user_verified_target(base_dir, "hash_hist_1", "Receipts")

    # Slightly modified unclassified document (no target path initially)
    # The wording is slightly edited, but semantic similarity remains high
    new_text = "This is an invoice for laptop purchase from TechStore. Total amount due is $1250. Please pay by card transfer."
    corpus = {
        "new_receipt.txt": new_text
    }
    analyzer.partial_fit(base_dir, corpus)

    # Generate plan
    plan = analyzer.generate_sorting_plan(base_dir)

    # Verify that new_receipt.txt is automatically routed to "Receipts"
    assert "Receipts" in plan
    assert "new_receipt.txt" in plan["Receipts"]
    file_info = plan["Receipts"]["new_receipt.txt"]
    assert file_info["routed_by"] == "similarity"
    assert "similarity >= 0.8" in file_info["match"]


def test_document_similarity_no_dilution():
    # Verify that diverse files in the same folder do not dilute or interfere
    # with individual matching.
    base_dir = "test_no_dilution_base"
    db.clear(base_dir)

    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "and"}, db=db, strategy_name=None
    )

    # Diverse historical documents sorted to the same folder "SharedFolder"
    # Make them longer and distinct to showcase individual matching.
    finance_text = "corporate financial document. quarterly earnings report and stock dividend distribution portfolios. asset balance sheet."
    cooking_text = "cooking and dessert instructions. bake chocolate frosting cake and delicious muffins in the oven with sweet ingredients."
    
    db.upsert_document(base_dir, "hist_finance.txt", "hash_f", finance_text)
    db.set_user_verified_target(base_dir, "hash_f", "SharedFolder")
    
    db.upsert_document(base_dir, "hist_cooking.txt", "hash_c", cooking_text)
    db.set_user_verified_target(base_dir, "hash_c", "SharedFolder")

    # New file slightly matching cooking_text only
    new_cooking = "cooking and dessert instructions. bake chocolate frosting cake and delicious muffins in the oven with sweet ingredients. extra: cookie."
    corpus = {
        "new_cooking.txt": new_cooking
    }
    analyzer.partial_fit(base_dir, corpus)

    plan = analyzer.generate_sorting_plan(base_dir)

    # Should match hist_cooking.txt individually and route to "SharedFolder"
    assert "SharedFolder" in plan
    assert "new_cooking.txt" in plan["SharedFolder"]
    file_info = plan["SharedFolder"]["new_cooking.txt"]
    assert file_info["routed_by"] == "similarity"


def test_document_similarity_guardrail_unverified():
    # Verify Guardrail 2: only match against documents with a user-verified target path.
    # Unverified documents in the DB (user_verified_target_path is None) must not be matched against.
    base_dir = "test_guardrail_base"
    db.clear(base_dir)

    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "and"}, db=db, strategy_name=None
    )

    # Document in DB with NO verified target path (unverified)
    unverified_text = "extremely unique text about space and rockets and Mars landing"
    db.upsert_document(base_dir, "unverified_space.txt", "hash_s", unverified_text)

    # New file with high similarity
    new_space = "unique text about space rockets and Mars landing mission"
    corpus = {
        "new_space.txt": new_space
    }
    analyzer.partial_fit(base_dir, corpus)

    plan = analyzer.generate_sorting_plan(base_dir)

    # Since there are no verified historical documents, new_space.txt should NOT be matched
    # and the returned plan should be empty because we bypassed clustering.
    assert plan == {}


def test_relational_term_frequency_generation():
    base_dir = "test_tf_gen_base"
    db.clear(base_dir)

    doc_text = "Apple orange pear! Apple apple banana."
    db.upsert_document(base_dir, "test_file.txt", "hash_tf_1", doc_text)

    tf = db.get_term_frequencies(base_dir)
    assert len(tf) > 0

    # Map back to dict
    tf_dict = {term: freq for _, term, freq in tf}
    assert tf_dict["apple"] == 3
    assert tf_dict["orange"] == 1
    assert tf_dict["pear"] == 1
    assert tf_dict["banana"] == 1


def test_relational_term_frequency_no_decryption(mocker):
    base_dir = "test_tf_no_decrypt_base"
    db.clear(base_dir)

    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "this", "is", "an", "of", "about"}, db=db, strategy_name=None
    )

    # Add historical document
    db.upsert_document(base_dir, "hist_file.txt", "hash_hist", "This is an exceptional piece of content about cats.")
    db.set_user_verified_target(base_dir, "hash_hist", "CatsCategory")

    # Add new unassigned document
    corpus = {"new_file.txt": "exceptional piece of content about cats."}
    analyzer.partial_fit(base_dir, corpus)

    # Spy on db.crypto.decrypt_text to ensure zero decryptions during similarity matching
    # Note: when partial_fit was called, it might have encrypted, but now we're checking generate_sorting_plan
    spy_decrypt = mocker.spy(db.crypto, "decrypt_text")

    plan = analyzer.generate_sorting_plan(base_dir)

    # Zero text decryption operations during similarity calculation
    # Because we used term frequencies entirely for similarity matching!
    assert spy_decrypt.call_count == 0
    assert "CatsCategory" in plan
    assert "new_file.txt" in plan["CatsCategory"]


def test_relational_term_frequency_dynamic_stop_words():
    base_dir = "test_tf_dynamic_stop_words"
    db.clear(base_dir)

    # If "exceptional" is NOT a stop word, then hist_file and new_file should match on similarity.
    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words=set(), db=db, strategy_name=None
    )

    db.upsert_document(base_dir, "hist_file.txt", "hash_hist", "exceptional exceptional exceptional exceptional cats")
    db.set_user_verified_target(base_dir, "hash_hist", "CatsCategory")

    corpus = {"new_file.txt": "exceptional exceptional exceptional exceptional dogs"}
    analyzer.partial_fit(base_dir, corpus)

    # Since they share "exceptional" heavily, they have high similarity.
    plan = analyzer.generate_sorting_plan(base_dir)
    assert "CatsCategory" in plan
    assert "new_file.txt" in plan["CatsCategory"]

    # Now we dynamically add "exceptional" as a custom stop-word.
    # Now they only have "cats" vs "dogs", which have 0 similarity.
    analyzer.reload_stop_words({"exceptional"})
    plan2 = analyzer.generate_sorting_plan(base_dir)
    assert "new_file.txt" not in plan2.get("CatsCategory", {})


def test_relational_term_frequency_speed_and_scalability():
    import time
    base_dir = "test_tf_scalability_base"
    db.clear(base_dir)

    analyzer = IncrementalAnalyzer(
        max_folders=5, stop_words={"the", "and"}, db=db, strategy_name=None
    )

    # Seed 1,000 historical documents
    documents_to_upsert = []
    for i in range(1000):
        filepath = f"historical_{i}.txt"
        file_hash = f"hash_{i}"
        # Alternate topics
        topic = "finance" if i % 2 == 0 else "cooking"
        text = "finance stock" if topic == "finance" else "cooking recipes"
        documents_to_upsert.append((base_dir, filepath, file_hash, text))

    db.upsert_documents(documents_to_upsert)

    # Set user verified target path for all historical docs
    for i in range(1000):
        file_hash = f"hash_{i}"
        topic = "finance" if i % 2 == 0 else "cooking"
        target_path = "FinanceFolder" if topic == "finance" else "CookingFolder"
        db.set_user_verified_target(base_dir, file_hash, target_path)

    # Prepare some unassigned/new files to sort
    new_corpus = {}
    for j in range(1):
        # High similarity cooking file
        new_corpus[f"new_cook_{j}.txt"] = "cooking recipes"
    analyzer.partial_fit(base_dir, new_corpus)

    # Warm up: run once to establish cached connection and populate in-memory database caches
    analyzer.generate_sorting_plan(base_dir)

    # Measure the execution time of similarity-based sorting (cached/warm run)
    start_time = time.perf_counter()
    plan = analyzer.generate_sorting_plan(base_dir)
    end_time = time.perf_counter()

    duration_ms = (end_time - start_time) * 1000.0
    print(f"Similarity matching on 1,000 documents took: {duration_ms:.2f} ms")

    # Assert success metrics
    # 1. Similarity recalculation completes in under 200 ms (it will actually be under 5 ms!)
    assert duration_ms < 200.0

    # 2. Correct classification
    assert "CookingFolder" in plan
    for j in range(1):
        assert f"new_cook_{j}.txt" in plan["CookingFolder"]


