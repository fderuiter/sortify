import tempfile
from pathlib import Path
from types import SimpleNamespace
import pytest

from app.core.analyzer import IncrementalAnalyzer
from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager
from app.ui.app import find_and_remove_file, insert_file_into_plan, run_incremental_training_in_background

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
    from app.core.db_conn import clear_connection_cache

    clear_connection_cache()
    import shutil

    if _test_dir:
        shutil.rmtree(_test_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_db():
    db.clear()
    yield


def test_rating_persistence_and_migration():
    # Verify migration executed and rating column exists and holds text
    db.upsert_document("base", "file1.txt", "hash1", "Some document content")
    
    # Set document rating
    db.set_document_rating("base", "file1.txt", "positive")
    
    # Retrieve documents and check rating is set
    ratings = db.get_all_document_ratings("base")
    assert len(ratings) == 1
    assert ratings["file1.txt"] == "positive"

    # Set document rating by hash
    db.set_document_rating_by_hash("base", "hash1", "negative")
    ratings = db.get_all_document_ratings("base")
    assert ratings["file1.txt"] == "negative"

    # Set user verified target path by filepath
    db.set_user_verified_target_path("base", "file1.txt", "Finance")
    docs = db.get_all_documents("base")
    assert docs[0][3] == "Finance"


def test_find_and_remove_and_insert_helpers():
    plan = {
        "Finance": {
            "Invoices": {
                "invoice.pdf": {"__type__": "file", "status": "Proposed"}
            }
        }
    }

    # Find and remove
    file_info = find_and_remove_file(plan, "invoice.pdf")
    assert file_info == {"__type__": "file", "status": "Proposed"}
    assert plan == {}  # cleaned all the way up because they became empty!

    # Insert file back
    insert_file_into_plan(plan, "Accounting/SubFolder", "invoice.pdf", file_info)
    assert plan["Accounting"]["SubFolder"]["invoice.pdf"] == {"__type__": "file", "status": "Proposed"}


def test_analyzer_locked_files_override():
    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "and"}, db=db, model_path=None
    )
    
    corpus = {
        "file1.txt": "Semantic content here.",
        "file2.txt": "Semantic content there.",
    }
    analyzer.partial_fit("dummy_base", corpus)

    settings = SimpleNamespace(KEYWORD_RULES={"file1": "KeywordFolder"}, POLICIES=[])

    # Case 1: normal routing (keyword matches)
    plan = analyzer.generate_sorting_plan("dummy_base", settings)
    assert "KeywordFolder" in plan
    assert "file1.txt" in plan["KeywordFolder"]

    # Case 2: locked_files override forces a different target folder, bypassing rules
    locked_files = {"file1.txt": "ManualOverrideFolder"}
    plan_with_override = analyzer.generate_sorting_plan("dummy_base", settings, locked_files=locked_files)
    
    assert "KeywordFolder" not in plan_with_override or "file1.txt" not in plan_with_override.get("KeywordFolder", {})
    assert "ManualOverrideFolder" in plan_with_override
    assert "file1.txt" in plan_with_override["ManualOverrideFolder"]


def test_incremental_background_training():
    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "and"}, db=db, model_path=None
    )
    
    # 1. Upsert some documents and specify verified target path
    db.upsert_document("dummy_train_base", "invoice.txt", "hash_inv", "Invoice content details")
    db.set_user_verified_target_path("dummy_train_base", "invoice.txt", "Accounting")

    # Set up mock session
    class MockSession:
        def __init__(self, db_inst, analyzer_inst):
            self.db = db_inst
            self.analyzer = analyzer_inst

    session = MockSession(db, analyzer)

    # 2. Check that no vector exists for invoice.txt in document_vectors yet
    assert db.get_document_vector("dummy_train_base", "invoice.txt") is None

    # 3. Run background training helper
    run_incremental_training_in_background(session, "dummy_train_base")

    # 4. Check that vector was generated and saved
    vector = db.get_document_vector("dummy_train_base", "invoice.txt")
    assert vector is not None
    assert len(vector) > 0
