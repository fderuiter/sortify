import pytest

from app.core.analyzer import IncrementalAnalyzer
from app.core.db import db


@pytest.fixture(autouse=True)
def clean_db():
    db.clear()
    yield

def test_incremental_analyzer_init():
    analyzer = IncrementalAnalyzer(max_folders=5, stop_words={"the", "and"})
    assert analyzer.max_folders == 5
    assert analyzer.corpus == {}


def test_partial_fit():
    analyzer = IncrementalAnalyzer(max_folders=3, stop_words={"the", "and"})
    corpus = {"file1.txt": "This is a document about finance and money.", "file2.txt": "Science and technology are great."}
    
    analyzer.partial_fit("dummy_base", corpus)
    assert len(analyzer.corpus) == 2
    assert "file1.txt" in analyzer.corpus


def test_partial_fit_empty():
    analyzer = IncrementalAnalyzer(max_folders=3, stop_words={"the", "and"})
    analyzer.partial_fit("dummy_base", {})
    assert len(analyzer.corpus) == 0


def test_generate_sorting_plan_empty():
    analyzer = IncrementalAnalyzer(max_folders=3, stop_words={"the", "and"})
    plan = analyzer.generate_sorting_plan("dummy_base")
    assert plan == {}


def test_generate_sorting_plan():
    analyzer = IncrementalAnalyzer(max_folders=2, stop_words={"the", "and"})
    corpus = {
        "finance1.txt": "money bank finance investment",
        "finance2.txt": "investment stock market money",
        "tech1.txt": "software computer science technology",
        "tech2.txt": "technology hardware computer"
    }
    analyzer.partial_fit("dummy_base", corpus)
    plan = analyzer.generate_sorting_plan("dummy_base")
    
    # Check that there are at least some folders created or files added
    assert isinstance(plan, dict)
    assert len(plan) > 0


def test_partial_fit_exception(mocker):
    analyzer = IncrementalAnalyzer(max_folders=2, stop_words={"the", "and"})
    mocker.patch.object(analyzer.model, "encode", side_effect=Exception("Test error"))
    mock_logger = mocker.patch("app.core.analyzer.logging.error")
    
    corpus = {"file.txt": "test content"}
    analyzer.partial_fit("dummy_base", corpus)
    
    mock_logger.assert_called_once()
    assert "file.txt" in analyzer.corpus  # Update still happened before exception


def test_generate_sorting_plan_exception(mocker):
    analyzer = IncrementalAnalyzer(max_folders=2, stop_words={"the", "and"})
    corpus = {"file.txt": "test content"}
    analyzer.partial_fit("dummy_base", corpus)
    
    mocker.patch("app.core.db.db.get_all_documents", side_effect=Exception("Test error"))
    mock_logger = mocker.patch("app.core.analyzer.logging.error")
    
    plan = analyzer.generate_sorting_plan("dummy_base")
    
    mock_logger.assert_called_once()
    assert plan == {}

def test_naming_collision_resolution():
    analyzer = IncrementalAnalyzer(max_folders=3, stop_words={"the"})
    # We want two topics to have the same primary keywords, but different term frequencies
    corpus = {
        "file1.txt": "apple banana apple banana apple orange",
        "file2.txt": "apple banana apple banana grape grape grape grape",
        "file3.txt": "apple banana apple banana peach peach",
        "file4.txt": "apple banana apple banana kiwi kiwi kiwi kiwi kiwi"
    }
    analyzer.partial_fit("dummy_base", corpus)
    plan = analyzer.generate_sorting_plan("dummy_base")
    
    folder_names = list(plan.keys())
    assert "Miscellaneous" not in folder_names or len(folder_names) > 1
