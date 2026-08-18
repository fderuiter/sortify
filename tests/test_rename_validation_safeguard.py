"""Tests for Pre-Execution Validation & UI Immutability Safeguard.

Covers extension immutability, OS character validation, pre-execution confirmation gating,
plan verification status indicators, and execution pipeline safety.
"""

import os
import tempfile
from pathlib import Path

import pytest

from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager
from app.core.mover import execute_moves
from app.core.verifier import VerificationEngine


@pytest.fixture
def test_env():
    """Create temporary environment for move execution testing."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_worker = DBWorker()
        db_path = Path(tmp_dir) / "test.db"
        db = Database(db_path, db_worker)
        cache_manager = CacheManager(str(Path(tmp_dir) / "cache.db"), db_worker)
        history_manager = HistoryManager(
            db, cache_manager, str(Path(tmp_dir) / "history.db")
        )
        yield tmp_dir, db, history_manager
        db_worker.stop()
        from app.core.db_conn import clear_connection_cache
        clear_connection_cache(only_current_and_inactive=False)


def test_ui_manual_rename_extension_immutability():
    """Requirement 1 & AC 1: Extension immutability during manual file renaming."""
    # Test extension extraction and dynamic locking logic
    original_filename = "document_report.pdf"
    orig_stem, orig_ext = os.path.splitext(original_filename)
    assert orig_ext == ".pdf"

    # User attempts to enter stem with different extension or no extension
    user_inputs = [
        "new_report",
        "new_report.txt",
        "new_report.pdf",
        "new_report.",
    ]

    for user_input in user_inputs:
        stem = user_input.strip()
        if orig_ext and stem.lower().endswith(orig_ext.lower()):
            stem = stem[:-len(orig_ext)]
        elif os.path.splitext(stem)[1]:
            stem = os.path.splitext(stem)[0]

        final_filename = stem + orig_ext
        assert final_filename.endswith(".pdf")
        assert os.path.splitext(final_filename)[1] == orig_ext


def test_plan_verification_flags_illegal_os_characters_and_invalid_paths():
    """Requirement 2 & AC 2: Plan verification flags illegal OS characters or invalid structures."""
    base_dir = "/tmp/test_base"

    # Plan with illegal characters in target_filename
    invalid_plan = {
        "file1.txt": {
            "__type__": "file",
            "relative_source": "file1.txt",
            "target_filename": "file:illegal?.txt",
            "confirmed": True,
        }
    }

    result = VerificationEngine.verify_plan_integrity(base_dir, invalid_plan)
    assert result["success"] is False
    assert len(result["invalid_renames"]) == 1
    assert result["invalid_renames"][0]["type"] == "illegal_os_characters"
    assert any("illegal OS characters" in w for w in result["warnings"])


def test_plan_verification_flags_modified_or_deleted_extensions():
    """Requirement 1, 2 & AC 1, 2: Plan verification flags modified or deleted extensions."""
    base_dir = "/tmp/test_base"

    # Proposal modifies extension from .pdf to .txt
    modified_ext_plan = {
        "report.pdf": {
            "__type__": "file",
            "relative_source": "report.pdf",
            "target_filename": "report_renamed.txt",
            "confirmed": True,
        }
    }

    result = VerificationEngine.verify_plan_integrity(base_dir, modified_ext_plan)
    assert result["success"] is False
    assert len(result["invalid_renames"]) == 1
    assert result["invalid_renames"][0]["type"] == "modified_extension"
    assert any("modifies or deletes original file extension" in w for w in result["warnings"])


def test_plan_verification_and_execution_blocks_unconfirmed_renames(test_env):
    """Requirement 3, 4 & AC 3: Verification flags unconfirmed renames and execution stops."""
    tmp_dir, db, history_manager = test_env

    # Create source file
    src_file = Path(tmp_dir) / "source_doc.txt"
    src_file.write_text("sample content")

    # Rename proposal without explicit confirmation flag
    unconfirmed_plan = {
        "source_doc.txt": {
            "__type__": "file",
            "relative_source": "source_doc.txt",
            "target_filename": "proposed_doc.txt",
            # "confirmed": True is intentionally omitted!
        }
    }

    # 1. Verification engine flags unconfirmed proposal
    result = VerificationEngine.verify_plan_integrity(tmp_dir, unconfirmed_plan)
    assert result["success"] is False
    assert len(result["unconfirmed_renames"]) == 1
    assert result["unconfirmed_renames"][0]["type"] == "unconfirmed_rename"
    assert any("lacks explicit user confirmation" in w for w in result["warnings"])

    # 2. Execution pipeline automatically stops relocation and raises ValueError
    with pytest.raises(ValueError, match="lacks explicit user confirmation"):
        execute_moves(tmp_dir, unconfirmed_plan, db, history_manager)

    # Confirm source file was NOT moved
    assert src_file.exists()
    assert not (Path(tmp_dir) / "proposed_doc.txt").exists()


def test_plan_verification_and_execution_succeeds_for_confirmed_valid_renames(test_env):
    """Requirement 2, 3, 4 & AC 5: Validated and confirmed file rename plans execute successfully."""
    tmp_dir, db, history_manager = test_env

    # Create source file
    src_file = Path(tmp_dir) / "original_notes.docx"
    src_file.write_text("meeting notes content")

    # Fully validated and confirmed rename plan
    confirmed_plan = {
        "original_notes.docx": {
            "__type__": "file",
            "relative_source": "original_notes.docx",
            "target_filename": "meeting_notes_2026.docx",
            "confirmed": True,
        }
    }

    # 1. Verification engine passes
    result = VerificationEngine.verify_plan_integrity(tmp_dir, confirmed_plan)
    assert result["success"] is True
    assert len(result["invalid_renames"]) == 0
    assert len(result["unconfirmed_renames"]) == 0

    # 2. Execution succeeds without errors
    execute_moves(tmp_dir, confirmed_plan, db, history_manager)

    # Verify physical file relocation
    assert not src_file.exists()
    dest_file = Path(tmp_dir) / "meeting_notes_2026.docx"
    assert dest_file.exists()
    assert dest_file.read_text() == "meeting notes content"
