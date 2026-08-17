import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from app.config import AppSettings
from app.core.session import AppSession
from app.core.metadata import MetadataPass
from app.core.analyzer import IncrementalAnalyzer
from app.core.history import HistoryManager


@pytest.fixture
def temp_environment():
    """Create a temporary directory structure representing a scan directory."""
    with tempfile.TemporaryDirectory() as temp_dir:
        base_path = Path(temp_dir)
        
        # Source directory for sorting
        src_dir = base_path / "source"
        src_dir.mkdir()
        
        # Files for fast-path rules
        policy_file = src_dir / "compliance_report.pdf"
        policy_file.write_text("This contains compliance override content.")
        
        keyword_file = src_dir / "invoice_12345.txt"
        keyword_file.write_text("Invoice standard content.")
        
        # File for AI / slow-path (does not match any keyword/policy)
        ai_file = src_dir / "unknown_document.txt"
        ai_file.write_text("Deep neural network document with high entropy content.")

        yield {
            "base_dir": str(src_dir),
            "policy_file": "compliance_report.pdf",
            "keyword_file": "invoice_12345.txt",
            "ai_file": "unknown_document.txt",
        }


def test_fast_path_plan_isolation(temp_environment):
    """Verify that generate_sorting_plan(fast_path_only=True) returns ONLY deterministic matches and bypasses AI/ML."""
    settings = AppSettings()
    
    # Configure deterministic rules
    settings.POLICIES = [
        {
            "type": "override",
            "expression": "compliance",
            "target_path": "Compliance",
            "priority": 10,
        }
    ]
    settings.KEYWORD_RULES = {
        "invoice": "Invoices"
    }
    settings.LEARNED_RULES = {}

    session = AppSession(settings, base_dir=temp_environment["base_dir"])
    
    # Populate documents into the DB (simulating scan & fit)
    docs = [
        (temp_environment["base_dir"], temp_environment["policy_file"], "hash1", "compliance report"),
        (temp_environment["base_dir"], temp_environment["keyword_file"], "hash2", "invoice details"),
        (temp_environment["base_dir"], temp_environment["ai_file"], "hash3", "unknown deep content"),
    ]
    session.db.upsert_documents(docs)

    # 1. Generate plan with fast_path_only=True
    # We patch vector matching and clustering to ensure they are NOT even called!
    with mock.patch.object(session.analyzer.embedding_manager, "get_vectors_batch") as mock_get_vectors:
        fast_path_plan = session.generate_sorting_plan(fast_path_only=True)
        
        # Verify AI/ML vectors were bypassed completely
        mock_get_vectors.assert_not_called()

    # Verify that the fast_path_plan only contains the deterministic rule matches
    # compliance_report.pdf matches policy "compliance" -> "Compliance"
    # invoice_12345.txt matches keyword "invoice" -> "Invoices"
    # unknown_document.txt does NOT match fast-path rules and is omitted!
    assert "Compliance" in fast_path_plan
    assert "Invoices" in fast_path_plan
    assert "Miscellaneous" not in fast_path_plan
    
    assert temp_environment["policy_file"] in fast_path_plan["Compliance"]
    assert temp_environment["keyword_file"] in fast_path_plan["Invoices"]
    
    # AI file should NOT be in the fast-path plan
    for folder, files in fast_path_plan.items():
        assert temp_environment["ai_file"] not in files

    # 2. Generate plan with fast_path_only=False (includes AI/Miscellaneous)
    slow_path_plan = session.generate_sorting_plan(fast_path_only=False)
    # The slow-path plan should contain the remaining unorganized file in Miscellaneous/AI strategies
    all_files_in_slow_plan = []
    for folder, files in slow_path_plan.items():
        all_files_in_slow_plan.extend(files.keys())
    assert temp_environment["ai_file"] in all_files_in_slow_plan

    session.close()


def test_ocr_bypassing_for_fast_path_moved_files(temp_environment):
    """Verify that text extraction and OCR processing are bypassed for files successfully moved in the fast-path."""
    settings = AppSettings()
    settings.KEYWORD_RULES = {"invoice": "Invoices"}
    settings.POLICIES = []
    settings.LEARNED_RULES = {}

    session = AppSession(settings, base_dir=temp_environment["base_dir"])

    # First metadata pass identifies the rule-matched file as bypassed
    files = [temp_environment["policy_file"], temp_environment["keyword_file"], temp_environment["ai_file"]]
    bypassed_files = MetadataPass.run(
        temp_environment["base_dir"], files, settings, session.db, None, lambda: False
    )

    # invoice_12345.txt matches "invoice" rule -> bypassed
    assert temp_environment["keyword_file"] in bypassed_files
    assert temp_environment["ai_file"] not in bypassed_files

    # Generate and execute fast-path moves
    fast_path_plan = session.generate_sorting_plan(fast_path_only=True)
    assert temp_environment["keyword_file"] in fast_path_plan.get("Invoices", {})
    
    session.execute_moves(fast_path_plan)
    
    # The file invoice_12345.txt has been successfully moved to Invoices/invoice_12345.txt
    moved_path = os.path.join(temp_environment["base_dir"], "Invoices", temp_environment["keyword_file"])
    assert os.path.exists(moved_path)

    # Refresh scanned directory state & invalidate cache
    session.db.invalidate_cache()
    from app.core.scanner import get_files_recursively
    refreshed_files = get_files_recursively(temp_environment["base_dir"])
    
    # In Phase 2, we run MetadataPass.run on refreshed files.
    # Since invoice_12345.txt was moved, its target path in DB has been updated.
    # We must ensure that we do NOT process it for OCR / text extraction.
    bypassed_files_ph2 = MetadataPass.run(
        temp_environment["base_dir"], refreshed_files, settings, session.db, None, lambda: False
    )
    
    # Normalize paths for platform-agnostic assertion
    refreshed_files_norm = [f.replace("\\", "/") for f in refreshed_files]
    bypassed_files_ph2_norm = [f.replace("\\", "/") for f in bypassed_files_ph2]
    
    # The successfully moved file is marked as bypassed or is in a target directory
    # and should NOT undergo text extraction / OCR
    rel_moved_path = os.path.join("Invoices", temp_environment["keyword_file"]).replace("\\", "/")
    assert rel_moved_path in bypassed_files_ph2_norm

    # Check that the items to sort (which undergo heavy text extraction) do NOT contain the moved file!
    items_to_sort_ph2 = [f for f in refreshed_files_norm if f not in bypassed_files_ph2_norm]
    assert rel_moved_path not in items_to_sort_ph2

    session.close()


def test_independent_rollback_safety(temp_environment):
    """Verify that a failure during Phase 2 (AI phase) allows a full rollback back to the clean state right after Phase 1 completed."""
    settings = AppSettings()
    settings.KEYWORD_RULES = {"invoice": "Invoices"}
    settings.POLICIES = []
    
    session = AppSession(settings, base_dir=temp_environment["base_dir"])
    
    # 1. Execute Phase 1 (Fast-Path)
    # Populate the database
    docs = [
        (temp_environment["base_dir"], temp_environment["policy_file"], "hash1", "compliance"),
        (temp_environment["base_dir"], temp_environment["keyword_file"], "hash2", "invoice"),
        (temp_environment["base_dir"], temp_environment["ai_file"], "hash3", "unknown"),
    ]
    session.db.upsert_documents(docs)
    
    fast_path_plan = session.generate_sorting_plan(fast_path_only=True)
    # This executes fast path, records the first snapshot, and commits
    session.execute_moves(fast_path_plan)
    
    # Verify invoice was moved in fast-path
    invoice_target_path = os.path.join(temp_environment["base_dir"], "Invoices", temp_environment["keyword_file"])
    assert os.path.exists(invoice_target_path)
    assert not os.path.exists(os.path.join(temp_environment["base_dir"], temp_environment["keyword_file"]))
    
    # 2. Setup Phase 2 (Slow-Path AI) and simulate a failure during moves
    session.db.invalidate_cache()
    slow_path_plan = session.generate_sorting_plan(fast_path_only=False)
    
    # We patch execute_moves to raise an exception mid-way to simulate a failure (e.g. power outage or crash)
    # Wait, let's inject a fake move in the slow_path_plan that triggers an exception
    # or mock _execute_moves_recursive to raise an OSError.
    with mock.patch("app.core.mover._execute_moves_recursive", side_effect=OSError("Disk failure during AI phase moves")):
        with pytest.raises(OSError):
            session.execute_moves(slow_path_plan)

    # After the exception, execute_moves's rollback catcher must have rolled back Phase 2's snapshot,
    # restoring the directory state to EXACTLY the state right after Phase 1 completed!
    # Let's verify that:
    # - invoice_12345.txt is STILL in its correct Invoices/ folder (from the committed Phase 1)
    assert os.path.exists(invoice_target_path)
    
    # - unknown_document.txt is STILL in its original unorganized path (since AI move failed and rolled back)
    assert os.path.exists(os.path.join(temp_environment["base_dir"], temp_environment["ai_file"]))

    session.close()
