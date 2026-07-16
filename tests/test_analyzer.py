from app.core.analyzer import IncrementalAnalyzer
import numpy as np
import pytest


from app.core.db import db


@pytest.fixture(autouse=True)
def clean_db():
    db.clear()
    yield


def test_incremental_analyzer_init():
    analyzer = IncrementalAnalyzer(max_folders=5, stop_words={"the", "and"}, model_path="all-MiniLM-L6-v2")
    assert analyzer.max_folders == 5
    assert analyzer.corpus == {}


def test_partial_fit():
    analyzer = IncrementalAnalyzer(max_folders=3, stop_words={"the", "and"}, model_path="all-MiniLM-L6-v2")
    corpus = {
        "file1.txt": "This is a document about finance and money.",
        "file2.txt": "Science and technology are great.",
    }

    analyzer.partial_fit("dummy_base", corpus)
    assert len(analyzer.corpus) == 2
    assert "file1.txt" in analyzer.corpus


def test_partial_fit_empty():
    analyzer = IncrementalAnalyzer(max_folders=3, stop_words={"the", "and"}, model_path="all-MiniLM-L6-v2")
    analyzer.partial_fit("dummy_base", {})
    assert len(analyzer.corpus) == 0

def test_mismatch_reembedding(mocker):
    # Setup first model
    analyzer1 = IncrementalAnalyzer(max_folders=3, stop_words=set(), model_path="all-MiniLM-L6-v2")
    corpus = {"test_doc.txt": "This is a test document."}
    
    # Spy on model.encode to see if it's called
    spy1 = mocker.spy(analyzer1.model, "encode")
    analyzer1.partial_fit("mismatch_base", corpus)
    assert spy1.call_count == 1
    
    # Verify it was saved with analyzer1's model name
    doc = db.get_document("mismatch_base", "test_doc.txt")
    assert doc is not None
    assert doc["model_name"] == "all-MiniLM-L6-v2"
    
    # Setup second model (simulating a model change)
    analyzer2 = IncrementalAnalyzer(max_folders=3, stop_words=set(), model_path="paraphrase-MiniLM-L3-v2")
    spy2 = mocker.spy(analyzer2.model, "encode")
    
    # Feed same corpus to partial_fit. In a normal cache hit, encode() is not called.
    # But since the model_name differs, it should call encode().
    analyzer2.partial_fit("mismatch_base", corpus)
    assert spy2.call_count == 1
    
    # Verify it updated the DB with the new model name
    doc = db.get_document("mismatch_base", "test_doc.txt")
    assert doc["model_name"] == "paraphrase-MiniLM-L3-v2"


def test_generate_sorting_plan_empty():
    analyzer = IncrementalAnalyzer(max_folders=3, stop_words={"the", "and"}, model_path="all-MiniLM-L6-v2")
    plan = analyzer.generate_sorting_plan("dummy_base")
    assert plan == {}


def test_generate_sorting_plan():
    analyzer = IncrementalAnalyzer(max_folders=2, stop_words={"the", "and"}, model_path="all-MiniLM-L6-v2")
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
    analyzer = IncrementalAnalyzer(max_folders=2, stop_words={"the", "and"}, model_path="all-MiniLM-L6-v2")
    mocker.patch.object(analyzer.model, "encode", side_effect=Exception("Test error"))
    mock_logger = mocker.patch("app.core.analyzer.logging.error")

    corpus = {"unique_file_exception.txt": "unique test content exception"}
    analyzer.partial_fit("dummy_base", corpus)

    mock_logger.assert_called_once()
    assert "unique_file_exception.txt" in analyzer.corpus  # Update still happened before exception


def test_generate_sorting_plan_exception(mocker):
    analyzer = IncrementalAnalyzer(max_folders=2, stop_words={"the", "and"}, model_path="all-MiniLM-L6-v2")
    corpus = {"file.txt": "test content"}
    analyzer.partial_fit("dummy_base", corpus)

    mocker.patch(
        "app.core.db.db.get_all_documents", side_effect=Exception("Test error")
    )
    mock_logger = mocker.patch("app.core.analyzer.logging.error")

    plan = analyzer.generate_sorting_plan("dummy_base")

    mock_logger.assert_called_once()
    assert plan == {}


def test_naming_collision_resolution():
    analyzer = IncrementalAnalyzer(max_folders=3, stop_words={"the"}, model_path="all-MiniLM-L6-v2")
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


from app.core.db import db
from app.core.cache import save_cache_sync
from types import SimpleNamespace

def test_conflict_detection():
    db.clear("test_conflict_base")
    # File matches both a keyword rule and has a historical override
    # keyword rule: "invoice" -> "Accounting"
    # historical override: "Archive"
    
    analyzer = IncrementalAnalyzer(max_folders=2, stop_words={"the", "and"})
    corpus = {"invoice_2025.txt": "Some invoice text"}
    
    # Put document in DB with an assigned folder (historical override)
    db.upsert_document("test_conflict_base", "invoice_2025.txt", "hash123", "Some invoice text", np.array([0.1]*384))
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
    
    analyzer = IncrementalAnalyzer(max_folders=2, stop_words={"the", "and"})
    corpus = {"invoice_2025.txt": "Some invoice text"}
    
    db.upsert_document("test_conflict_res_base", "invoice_2025.txt", "hash123", "Some invoice text", np.array([0.1]*384))
    db.set_user_verified_target("test_conflict_res_base", "hash123", "Archive")
    
    analyzer.partial_fit("test_conflict_res_base", corpus)
    
    # Pre-populate session cache with a locked choice
    locked_files = {"invoice_2025.txt": "Accounting"} # user chose compliance path
    save_cache_sync("test_conflict_res_base", corpus, locked_files, {}, set())
    
    settings = SimpleNamespace(KEYWORD_RULES={"invoice": "Accounting"})
    
    plan = analyzer.generate_sorting_plan("test_conflict_res_base", settings)
    
    # Since it was resolved to 'Accounting', it should be in Accounting and no longer flagged as conflicted
    assert "Accounting" in plan
    file_info = plan["Accounting"]["invoice_2025.txt"]
    assert file_info.get("is_conflicted", False) is False

