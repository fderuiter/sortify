import tempfile
from pathlib import Path

import pytest

from app.core.analyzer import IncrementalAnalyzer
from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager
from app.core.policy_engine import PolicyEngine

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


def test_policy_engine_standalone_evaluation():
    """Test that PolicyEngine can independently match compliance policies."""
    policies = [
        {
            "type": "keyword",
            "expression": "sensitive",
            "target_path": "Secure_Folder",
            "priority": 100,
        },
        {
            "type": "pattern",
            "expression": "restricted",
            "target_path": "Strict_Compliance",
            "priority": 50,
        },
    ]

    # Test keyword matching
    rule = PolicyEngine.evaluate_policies(
        "file.txt", "This is sensitive content", None, policies
    )
    assert rule is not None
    assert rule["target_path"] == "Secure_Folder"

    # Test pattern matching
    rule = PolicyEngine.evaluate_policies(
        "restricted_file.txt", "Some ordinary content", None, policies
    )
    assert rule is not None
    assert rule["target_path"] == "Strict_Compliance"

    # Test no match
    rule = PolicyEngine.evaluate_policies(
        "normal_file.txt", "No keywords here", None, policies
    )
    assert rule is None


def test_policy_engine_lock_path_validation():
    """Test folder lock paths schema validation in PolicyEngine."""
    # Valid paths should not raise anything
    PolicyEngine.validate_lock_path("Valid/Path/To/Folder")
    PolicyEngine.validate_lock_path("Another-Valid_Folder")

    # Invalid characters
    with pytest.raises(ValueError):
        PolicyEngine.validate_lock_path("Invalid:Folder")

    with pytest.raises(ValueError):
        PolicyEngine.validate_lock_path("Invalid?Folder")

    # Absolute path roots
    with pytest.raises(ValueError):
        PolicyEngine.validate_lock_path("/Absolute/Path")

    with pytest.raises(ValueError):
        PolicyEngine.validate_lock_path("\\Absolute\\Path")

    # Directory traversal
    with pytest.raises(ValueError):
        PolicyEngine.validate_lock_path("Some/../../Traversal")

    # Segment reserved device names
    with pytest.raises(ValueError):
        PolicyEngine.validate_lock_path("Valid/CON/Path")

    with pytest.raises(ValueError):
        PolicyEngine.validate_lock_path("Valid/AUX.txt/Path")

    # Segment trailing spaces or dots
    with pytest.raises(ValueError):
        PolicyEngine.validate_lock_path("Valid/Folder /Path")

    with pytest.raises(ValueError):
        PolicyEngine.validate_lock_path("Valid/Folder./Path")


def test_compliance_overrides_manual_lock():
    """Test that a matching high-priority policy overrides manual folder locks and flags it as corrected in the plan."""
    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "and"}, db=db, model_path=None
    )

    db.upsert_documents(
        [
            (
                "dummy",
                "confidential_finance.xlsx",
                "hash999",
                "Sensitive financial statements",
            )
        ]
    )

    class MockSettings:
        MAX_DEPTH = 5
        MAX_FEATURES = 3
        PRESERVE_HIERARCHY = False
        CONTEXTUAL_RENAMING = False
        POLICIES = [
            {
                "type": "keyword",
                "expression": "financial",
                "target_path": "Secure Finance",
                "priority": 500,
            },
        ]

    locked_files = {"confidential_finance.xlsx": "Public Shared Folder"}

    # Generate the sorting plan with locked_files
    plan = analyzer.generate_sorting_plan(
        "dummy", runtime_settings=MockSettings(), locked_files=locked_files
    )

    # The file should be routed to compliance-regulated folder ("Secure Finance") instead of "Public Shared Folder"
    assert "Secure Finance" in plan
    assert (
        "Public Shared Folder" not in plan
        or "confidential_finance.xlsx" not in plan.get("Public Shared Folder", {})
    )

    file_info = plan["Secure Finance"]["confidential_finance.xlsx"]
    assert file_info["__type__"] == "file"
    assert file_info["routed_by"] == "keyword"
    assert file_info["match"] == "financial"

    # Verify override / correction flags
    assert file_info.get("is_corrected") is True
    assert file_info.get("corrected") is True
    assert file_info.get("is_overridden") is True
    assert file_info.get("overridden") is True
    assert file_info.get("original_lock_path") == "Public Shared Folder"
    assert file_info.get("new_policy_path") == "Secure Finance"
    assert file_info.get("compliance_path") == "Secure Finance"


def test_invalid_lock_path_raises_during_plan_generation():
    """Test that invalid folder lock paths raise ValueError during plan generation."""
    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "and"}, db=db, model_path=None
    )

    db.upsert_documents([("dummy", "file.txt", "h1", "Some doc")])

    class MockSettings:
        MAX_DEPTH = 5
        MAX_FEATURES = 3
        PRESERVE_HIERARCHY = False
        CONTEXTUAL_RENAMING = False
        POLICIES = []

    # Try generating plan with directory traversal lock path
    locked_files = {"file.txt": "Some/../../Traversal"}

    with pytest.raises(ValueError):
        analyzer.generate_sorting_plan(
            "dummy", runtime_settings=MockSettings(), locked_files=locked_files
        )
