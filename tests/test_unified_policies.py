import tempfile
import logging
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
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


def test_policy_schema_validation():
    """Test that valid and invalid policies are correctly validated or rejected."""
    # Valid policies
    settings = Settings(
        POLICIES=[
            {"type": "keyword", "expression": "financial", "target_path": "Finance Folder", "priority": 10},
            {"type": "pattern", "expression": ".*report.*", "target_path": "Reports/Quarterly", "priority": 20},
            {"type": "override", "expression": "secret.txt", "target_path": "Restricted", "priority": 30},
        ]
    )
    assert len(settings.POLICIES) == 3

    # Reject invalid types
    with pytest.raises(ValidationError):
        Settings(POLICIES=[{"type": "invalid_type", "expression": "foo", "target_path": "bar", "priority": 1}])

    # Reject invalid expression (empty)
    with pytest.raises(ValidationError):
        Settings(POLICIES=[{"type": "keyword", "expression": "   ", "target_path": "bar", "priority": 1}])

    # Reject missing fields
    with pytest.raises(ValidationError):
        Settings(POLICIES=[{"type": "keyword", "expression": "foo", "priority": 1}])

    # Reject absolute path
    with pytest.raises(ValidationError):
        Settings(POLICIES=[{"type": "keyword", "expression": "foo", "target_path": "/absolute/path", "priority": 1}])

    # Reject directory traversal
    with pytest.raises(ValidationError):
        Settings(POLICIES=[{"type": "keyword", "expression": "foo", "target_path": "some/../../traversal", "priority": 1}])

    # Reject illegal characters
    with pytest.raises(ValidationError):
        Settings(POLICIES=[{"type": "keyword", "expression": "foo", "target_path": "some:bad?chars", "priority": 1}])


def test_policy_overlap_warnings(caplog):
    """Test that shadowing/overlapping rules trigger logged warning messages."""
    # Set up overlapping policies: A is a keyword and a substring of B (also keyword/pattern/override)
    with caplog.at_level(logging.WARNING):
        Settings(
            POLICIES=[
                {"type": "keyword", "expression": "compliance", "target_path": "Restricted", "priority": 10},
                {"type": "keyword", "expression": "compliance report", "target_path": "Reports", "priority": 5},
            ]
        )
    assert any("Rule overlap detected" in record.message for record in caplog.records)


def test_policy_priority_routing():
    """Test that rules are executed in strict priority order (higher priority first)."""
    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "and"}, db=db, model_path=None
    )

    corpus = {
        "financial_compliance_report.txt": "This is a financial compliance report document.",
    }
    analyzer.partial_fit("dummy", corpus)

    # First case: compliance rule has higher priority
    class MockSettings1:
        MAX_DEPTH = 5
        MAX_FEATURES = 3
        PRESERVE_HIERARCHY = False
        CONTEXTUAL_RENAMING = False
        POLICIES = [
            {"type": "keyword", "expression": "compliance", "target_path": "Compliance Folder", "priority": 100},
            {"type": "keyword", "expression": "financial", "target_path": "Financial Folder", "priority": 50},
        ]

    plan1 = analyzer.generate_sorting_plan("dummy", runtime_settings=MockSettings1())
    
    # Extract file assignment
    def find_folder_for(filename, p, current_path=""):
        if not isinstance(p, dict) or p.get("__type__") == "file":
            return None
        for k, v in p.items():
            if v is None or (isinstance(v, dict) and v.get("__type__") == "file"):
                if k == filename:
                    return current_path
            else:
                res = find_folder_for(
                    filename, v, current_path + "/" + k if current_path else k
                )
                if res:
                    return res
        return None

    assert find_folder_for("financial_compliance_report.txt", plan1) == "Compliance Folder"

    # Second case: financial rule has higher priority
    class MockSettings2:
        MAX_DEPTH = 5
        MAX_FEATURES = 3
        PRESERVE_HIERARCHY = False
        CONTEXTUAL_RENAMING = False
        POLICIES = [
            {"type": "keyword", "expression": "compliance", "target_path": "Compliance Folder", "priority": 50},
            {"type": "keyword", "expression": "financial", "target_path": "Financial Folder", "priority": 100},
        ]

    plan2 = analyzer.generate_sorting_plan("dummy", runtime_settings=MockSettings2())
    assert find_folder_for("financial_compliance_report.txt", plan2) == "Financial Folder"


def test_policy_override_bypasses_historical_and_ml():
    """Test that a matching high-priority policy bypasses user manual assignment (historical overrides) and ML."""
    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "and"}, db=db, model_path=None
    )

    # Put a document in the database and also set up a historical target path
    # Database document structure: (base_dir, filepath, file_hash, text)
    db.upsert_documents([
        ("dummy", "corp_restricted.xlsx", "hash123", "Highly confidential finance spreadsheet")
    ])
    db.set_user_verified_target("dummy", "hash123", "Manual User Folder")

    class MockSettings:
        MAX_DEPTH = 5
        MAX_FEATURES = 3
        PRESERVE_HIERARCHY = False
        CONTEXTUAL_RENAMING = False
        POLICIES = [
            {"type": "pattern", "expression": "restricted", "target_path": "Strict Compliance", "priority": 1000},
        ]

    plan = analyzer.generate_sorting_plan("dummy", runtime_settings=MockSettings())

    # The file has a manual historical assignment to "Manual User Folder",
    # but the compliance policy must override manual moves.
    def find_folder_for(filename, p, current_path=""):
        if not isinstance(p, dict) or p.get("__type__") == "file":
            return None
        for k, v in p.items():
            if v is None or (isinstance(v, dict) and v.get("__type__") == "file"):
                if k == filename:
                    return current_path
            else:
                res = find_folder_for(
                    filename, v, current_path + "/" + k if current_path else k
                )
                if res:
                    return res
        return None

    assert find_folder_for("corp_restricted.xlsx", plan) == "Strict Compliance"
