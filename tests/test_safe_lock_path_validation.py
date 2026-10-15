"""Tests for PolicyEngine safe lock path validation and TUI error reporting boundary."""

import os
from unittest.mock import MagicMock, patch

import pytest

from app.core.analyzer import IncrementalAnalyzer, SortingPlan
from app.core.domain_contracts import SortingPlanModel
from app.core.policy_engine import PolicyEngine


def test_validate_lock_path_safe_valid_and_invalid():
    """Verify PolicyEngine.validate_lock_path_safe behavior."""
    # Valid lock paths
    is_valid, err = PolicyEngine.validate_lock_path_safe("Valid/Path/To/Folder")
    assert is_valid is True
    assert err is None

    is_valid, err = PolicyEngine.validate_lock_path_safe("Another-Valid_Folder")
    assert is_valid is True
    assert err is None

    # Non-string input
    is_valid, err = PolicyEngine.validate_lock_path_safe(12345)
    assert is_valid is False
    assert "must be a string" in err

    # Traversal attack path
    is_valid, err = PolicyEngine.validate_lock_path_safe("Some/../../Traversal")
    assert is_valid is False
    assert err is not None

    # Absolute path
    is_valid, err = PolicyEngine.validate_lock_path_safe("/Absolute/Path")
    assert is_valid is False
    assert err is not None

    # Illegal character path
    is_valid, err = PolicyEngine.validate_lock_path_safe("Invalid?Folder")
    assert is_valid is False
    assert err is not None


def test_validate_lock_path_backward_compatibility():
    """Verify validate_lock_path maintains backward compatibility by raising ValueError."""
    # Valid path should pass without error
    PolicyEngine.validate_lock_path("Valid/Path/To/Folder")

    # Invalid path should raise ValueError
    with pytest.raises(ValueError):
        PolicyEngine.validate_lock_path("/Absolute/Path")


def test_sorting_plan_invalid_locks_property():
    """Verify SortingPlan and SortingPlanModel support invalid_locks property."""
    plan = SortingPlan(invalid_locks={"file1.pdf": "Invalid lock path: contains traversal"})
    assert plan.invalid_locks == {"file1.pdf": "Invalid lock path: contains traversal"}
    assert plan["invalid_locks"] == {"file1.pdf": "Invalid lock path: contains traversal"}
    assert plan.get("invalid_locks") == {"file1.pdf": "Invalid lock path: contains traversal"}

    # Model validation
    model = SortingPlanModel(invalid_locks={"doc.docx": "Illegal char"})
    assert model.invalid_locks == {"doc.docx": "Illegal char"}

    validated_plan = SortingPlan.model_validate(model)
    assert validated_plan.invalid_locks == {"doc.docx": "Illegal char"}


def test_incremental_analyzer_generate_sorting_plan_with_invalid_locks(tmp_path):
    """Verify IncrementalAnalyzer records invalid locks without throwing exceptions."""
    base_dir = str(tmp_path)

    # Create dummy files
    file1 = os.path.join(base_dir, "good.txt")
    with open(file1, "w") as f:
        f.write("Some document content")

    db_mock = MagicMock()
    db_mock.get_all_documents.return_value = [
        (file1, "Some document content", "hash123", None)
    ]

    analyzer = IncrementalAnalyzer(db=db_mock, max_folders=5, stop_words=set())

    locked_files = {
        file1: "Valid/Folder",
        "bad_file.txt": "/Absolute/Invalid/Path",
    }

    # Executing generate_sorting_plan should not raise ValueError
    plan = analyzer.generate_sorting_plan(
        base_dir, locked_files=locked_files, fast_path_only=True
    )

    assert isinstance(plan, SortingPlan)
    assert "bad_file.txt" in plan.invalid_locks
    assert plan.invalid_locks["bad_file.txt"] is not None


def test_tui_scan_worker_announces_skipped_invalid_locks():
    """Verify AutoSorterTUI.run_scan_worker handles invalid locks cleanly and announces skipped count."""
    from app.ui.tui import AutoSorterTUI

    settings_mock = MagicMock()
    app = AutoSorterTUI(settings=settings_mock)
    app.base_dir = "/tmp/fake_workspace"
    app.settings = MagicMock()

    mock_plan = SortingPlan(
        invalid_locks={"bad_doc.txt": "Path traversal detected"}
    )

    mock_session = MagicMock()
    mock_session.generate_sorting_plan.return_value = mock_plan

    announcements = []

    def fake_announce(msg):
        announcements.append(msg)

    with patch("app.core.session.AppSession", return_value=mock_session), \
         patch("app.core.scanner.get_files_from_sources", return_value=["bad_doc.txt"]), \
         patch("app.core.scanner.get_files_recursively", return_value=["bad_doc.txt"]), \
         patch("app.core.metadata.MetadataPass.run"), \
         patch.object(app, "call_from_thread", side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs) if callable(fn) else None), \
         patch.object(app, "announce", side_effect=fake_announce):

        app.run_scan_worker.__wrapped__(app)

    assert any("bad lock path was skipped" in msg for msg in announcements)
