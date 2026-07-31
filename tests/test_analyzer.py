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

    mocker.patch.object(db, "get_all_documents", side_effect=Exception("Test error"))
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

    # Initialize analyzer
    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "and"}, db=db
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
        max_folders=3, stop_words={"the", "and"}, db=db
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
        max_folders=3, stop_words={"the", "and"}, db=db
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
    # It should not be routed by similarity.
    # (Since there are no folders or rules, it may go to cluster-based plans or Miscellaneous)
    for folder in plan:
        if isinstance(plan[folder], dict) and "new_space.txt" in plan[folder]:
            file_info = plan[folder]["new_space.txt"]
            assert file_info is None or file_info.get("routed_by") != "similarity"

