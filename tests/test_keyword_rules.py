import tempfile
from pathlib import Path

import pytest

from app.core.analyzer import IncrementalAnalyzer
from app.core.cache import CacheManager
from app.core.db import Database
from app.core.history import HistoryManager

_test_dir = tempfile.mkdtemp()
db = Database(Path(_test_dir) / "test.db")
cache_manager = CacheManager(str(Path(_test_dir) / "cache.db"))
history_manager = HistoryManager(db, cache_manager, str(Path(_test_dir) / "history.db"))
def save_cache_sync(*args, **kwargs):
    cache_manager.save_cache_sync(*args, **kwargs)



@pytest.fixture(autouse=True)
def clean_db():
    db.clear()
    yield

def test_keyword_rules():
    analyzer = IncrementalAnalyzer(max_folders=3, stop_words={"the", "and"}, db=db, model_path=None)
    
    corpus = {
        "file1.txt": "Semantic content here.",
        "file2.txt": "Semantic content there.",
        "file3.txt": "Semantic content everywhere.",
        "report1.txt": "This is a report.",
        "report2.txt": "Another report.",
        "misc1.txt": "Random thing."
    }
    
    analyzer.partial_fit("dummy", corpus)
    
    class MockSettings:
        MAX_DEPTH = 5
        MAX_FEATURES = 3
        PRESERVE_HIERARCHY = False
        CONTEXTUAL_RENAMING = False
        KEYWORD_RULES = {"report": "Reports Folder"}
        
    # Since model is None, it returns a flat dict if no keyword rules apply. 
    # Let's use a real model or a mock strategy.
    analyzer = IncrementalAnalyzer(max_folders=3, stop_words={"the", "and"}, db=db, model_path="all-MiniLM-L6-v2")
    analyzer.partial_fit("dummy", corpus)
    plan = analyzer.generate_sorting_plan("dummy", runtime_settings=MockSettings())
    
    # plan is nested. We can extract all top-level keys
    print("Plan:", plan)
    
    def find_folder_for(filename, p, current_path=""):
        if not isinstance(p, dict) or p.get("__type__") == "file":
            return None
        for k, v in p.items():
            if v is None or (isinstance(v, dict) and v.get("__type__") == "file"):
                if k == filename:
                    return current_path
            else:
                res = find_folder_for(filename, v, current_path + "/" + k if current_path else k)
                if res:
                    return res
        return None

    rep1_folder = find_folder_for("report1.txt", plan)
    assert rep1_folder == "Reports Folder"
    
    file1_folder = find_folder_for("file1.txt", plan)
    assert file1_folder != "Miscellaneous"

def test_empty_keyword_rules_ignored():
    analyzer = IncrementalAnalyzer(max_folders=3, stop_words={"the", "and"}, db=db, model_path=None)
    
    corpus = {
        "file1.txt": "Semantic content here.",
        "file2.txt": "Semantic content there."
    }
    
    class MockSettings:
        MAX_DEPTH = 5
        MAX_FEATURES = 3
        PRESERVE_HIERARCHY = False
        CONTEXTUAL_RENAMING = False
        KEYWORD_RULES = {"": "Empty Folder", "   ": "Whitespace Folder", "file1": "Valid Folder"}
        
    analyzer.partial_fit("dummy", corpus)
    plan = analyzer.generate_sorting_plan("dummy", runtime_settings=MockSettings())
    
    def find_folder_for(filename, p, current_path=""):
        if not isinstance(p, dict) or p.get("__type__") == "file":
            return None
        for k, v in p.items():
            if v is None or (isinstance(v, dict) and v.get("__type__") == "file"):
                if k == filename:
                    return current_path
            else:
                res = find_folder_for(filename, v, current_path + "/" + k if current_path else k)
                if res:
                    return res
        return None

    file1_folder = find_folder_for("file1.txt", plan)
    assert file1_folder == "Valid Folder"
    
    file2_folder = find_folder_for("file2.txt", plan)
    # Since file2 has no valid matching rule, it shouldn't be in the empty or whitespace folder
    assert file2_folder not in ("Empty Folder", "Whitespace Folder")

