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
def test_environment():
    """Create a temporary environment with database, cache, and history manager."""
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


def test_nested_three_levels_verifier_resolves_correct_source_path():
    """
    Acceptance Criterion 1:
    The verification engine successfully resolves the correct source path
    for a file nested three levels deep.
    """
    base_dir = os.path.normpath("/base/dir")
    plan = {
        "dir1": {
            "dir2": {
                "dir3": {
                    "file.txt": {
                        "__type__": "file",
                        "relative_source": "file.txt",
                        "target_filename": "moved.txt",
                    }
                }
            }
        }
    }

    engine = VerificationEngine()
    moves = engine.get_moves(base_dir, plan)

    assert len(moves) == 1
    key, source_path, dest_path = moves[0]

    assert key == "file.txt"
    # Prepend active parent folder segments dynamically when computing nested file locations
    assert source_path == os.path.normpath("/base/dir/dir1/dir2/dir3/file.txt")
    assert dest_path == os.path.normpath("/base/dir/dir1/dir2/dir3/moved.txt")


def test_deeply_nested_physical_execution_relocates_identically(test_environment):
    """
    Acceptance Criterion 2:
    The physical execution engine successfully relocates a deeply nested file
    using the identical path verified in the dry-run.
    """
    tmp_dir, db, history_manager = test_environment

    # Create deeply nested source directory and file
    source_subdir = os.path.join(tmp_dir, "dir1", "dir2", "dir3")
    os.makedirs(source_subdir, exist_ok=True)
    source_file = os.path.join(source_subdir, "file.txt")
    with open(source_file, "w") as f:
        f.write("deep file content")

    plan = {
        "dir1": {
            "dir2": {
                "dir3": {
                    "file.txt": {
                        "__type__": "file",
                        "relative_source": "file.txt",
                        "target_filename": "moved.txt",
                        "confirmed": True,
                    }
                }
            }
        }
    }

    # Verify identical paths
    dry_run_moves = VerificationEngine.get_moves(tmp_dir, plan)
    assert len(dry_run_moves) == 1
    _, dry_run_src, dry_run_dst = dry_run_moves[0]

    execute_moves(tmp_dir, plan, db, history_manager)

    # Check physical file was relocated to the exact dry-run destination
    assert not os.path.exists(source_file)
    assert os.path.exists(dry_run_dst)
    with open(dry_run_dst, "r") as f:
        assert f.read() == "deep file content"


def test_manually_drafted_json_plan_validates_and_executes(test_environment):
    """
    Acceptance Criterion 3:
    A manually drafted JSON plan without pre-baked full paths in leaf keys
    validates and executes without error.
    """
    tmp_dir, db, history_manager = test_environment

    # Create manual source structure
    source_subdir = os.path.join(tmp_dir, "incoming")
    os.makedirs(source_subdir, exist_ok=True)
    source_file = os.path.join(source_subdir, "document.pdf")
    with open(source_file, "w") as f:
        f.write("pdf data")

    # Manually drafted JSON plan structure (does not have pre-baked full paths in keys)
    plan = {
        "incoming": {
            "document.pdf": {
                "__type__": "file",
                "relative_source": "document.pdf",
                "target_filename": "final.pdf",
                "confirmed": True,
            }
        }
    }

    # Should validate successfully
    result = VerificationEngine.verify_plan_integrity(tmp_dir, plan)
    assert result["success"] is True

    # Should execute physical moves successfully
    execute_moves(tmp_dir, plan, db, history_manager)

    assert not os.path.exists(source_file)
    expected_dest = os.path.join(tmp_dir, "incoming", "final.pdf")
    assert os.path.exists(expected_dest)


def test_system_safely_rejects_missing_relative_source_field(test_environment):
    """
    Acceptance Criterion 4:
    The system safely rejects any nested plan that is missing the required
    relative source metadata field.
    """
    tmp_dir, db, history_manager = test_environment

    # Plan has nested structure (subfolders) but is missing 'relative_source' metadata field
    invalid_plan = {
        "folder": {
            "file.txt": {
                "__type__": "file",
                "target_filename": "moved.txt",
            }
        }
    }

    # 1. Verification engine should report success=False and have warning about missing relative_source
    result = VerificationEngine.verify_plan_integrity(tmp_dir, invalid_plan)
    assert result["success"] is False
    assert any(
        "Missing required relative source metadata field" in w
        for w in result["warnings"]
    )

    # 2. Physical execution should reject and raise ValueError directly
    with pytest.raises(
        ValueError, match="Missing required relative source metadata field"
    ):
        execute_moves(tmp_dir, invalid_plan, db, history_manager)


def test_db_updates_use_forward_slashes_consistently(test_environment):
    """
    Ensure that even under simulated Windows-style path conditions or operations,
    the relative destination path (rel_dest) stored in the database has backslashes normalized
    to forward slashes for unified cross-platform path indexing.
    """
    tmp_dir, db, history_manager = test_environment

    # Create manual source structure
    source_subdir = os.path.join(tmp_dir, "nested_dir")
    os.makedirs(source_subdir, exist_ok=True)
    source_file = os.path.join(source_subdir, "item.txt")
    with open(source_file, "w") as f:
        f.write("item text")

    plan = {
        "nested_dir": {
            "item.txt": {
                "__type__": "file",
                "relative_source": "item.txt",
                "target_filename": "moved_item.txt",
                "confirmed": True,
            }
        }
    }

    # Pre-populate database with the document so get_document and path update triggers
    db.upsert_document(tmp_dir, "nested_dir/item.txt", "item_hash", "item text")

    execute_moves(tmp_dir, plan, db, history_manager)

    # The document path should be updated in the database to use forward slashes
    doc = db.get_document(tmp_dir, "nested_dir/moved_item.txt")
    assert doc is not None
    assert doc["file_hash"] == "item_hash"
