import logging
import os
from unittest.mock import patch

import pytest

from app.core.resilient_file_ops import (
    _cross_volume_atomic_move,
    atomic_move_non_clobber,
    resilient_move,
)


def test_atomic_move_non_clobber_prevents_overwrite(tmp_path):
    """Requirement 1: File move operations fail safely and do not overwrite target files when path collisions occur."""
    src = tmp_path / "document.txt"
    dst = tmp_path / "target_folder" / "document.txt"
    dst.parent.mkdir(parents=True, exist_ok=True)

    src.write_text("Source Data Payload", encoding="utf-8")
    dst.write_text("Pre-existing Target Payload", encoding="utf-8")

    # Attempting atomic non-clobber move must raise FileExistsError without modifying target or source
    with pytest.raises(FileExistsError):
        atomic_move_non_clobber(str(src), str(dst))

    assert dst.read_text(encoding="utf-8") == "Pre-existing Target Payload"
    assert src.read_text(encoding="utf-8") == "Source Data Payload"
    assert src.exists()


def test_resilient_move_dynamic_target_retry(tmp_path):
    """Requirement 2 & 3: Upon target collision, system automatically generates an incremented target path and retries."""
    dest_dir = tmp_path / "destination"
    dest_dir.mkdir(parents=True, exist_ok=True)

    src = tmp_path / "report.docx"
    dst = dest_dir / "report.docx"

    src.write_text("New Report Content", encoding="utf-8")
    dst.write_text("Existing Report Content", encoding="utf-8")

    # Call resilient_move; it should detect collision, dynamically re-calculate safe target name (report_1.docx), and succeed
    actual_target = resilient_move(str(src), str(dst))

    expected_target = str(dest_dir / "report_1.docx")
    assert os.path.normpath(actual_target) == os.path.normpath(expected_target)

    assert dst.read_text(encoding="utf-8") == "Existing Report Content"
    assert (dest_dir / "report_1.docx").read_text(
        encoding="utf-8"
    ) == "New Report Content"
    assert not src.exists()


def test_multiple_collisions_dynamic_suffix_increment(tmp_path):
    """Test multiple sequential collisions automatically increment numerical suffix (report_1.docx, report_2.docx)."""
    dest_dir = tmp_path / "destination"
    dest_dir.mkdir(parents=True, exist_ok=True)

    (dest_dir / "invoice.pdf").write_text("Original Invoice", encoding="utf-8")
    (dest_dir / "invoice_1.pdf").write_text("First Collision Invoice", encoding="utf-8")

    src = tmp_path / "invoice.pdf"
    src.write_text("Third Incoming Invoice", encoding="utf-8")

    actual_target = resilient_move(str(src), str(dest_dir / "invoice.pdf"))

    expected_target = str(dest_dir / "invoice_2.pdf")
    assert os.path.normpath(actual_target) == os.path.normpath(expected_target)
    assert (dest_dir / "invoice_2.pdf").read_text(
        encoding="utf-8"
    ) == "Third Incoming Invoice"
    assert not src.exists()


def test_cross_volume_atomic_move_verification_and_cleanup(tmp_path):
    """Requirement 4: Cross-volume moves verify target availability before finalizing source deletion and clean up transient staging files on failure."""
    src = tmp_path / "source" / "data.csv"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("CSV Content 123", encoding="utf-8")

    dst = tmp_path / "target_volume" / "data.csv"
    dst.parent.mkdir(parents=True, exist_ok=True)

    # Force cross-volume fallback path
    with patch("app.core.resilient_file_ops.IS_WINDOWS", False):
        with patch("os.link", side_effect=OSError(18, "Invalid cross-device link")):
            actual_dst = resilient_move(str(src), str(dst))

    assert os.path.normpath(actual_dst) == os.path.normpath(str(dst))
    assert dst.read_text(encoding="utf-8") == "CSV Content 123"
    assert not src.exists()

    # Verify transient staging files are cleaned up
    staging_files = [f for f in os.listdir(dst.parent) if f.startswith(".tmp_stage_")]
    assert len(staging_files) == 0


def test_cross_volume_collision_cleans_staging_and_preserves_source(tmp_path):
    """Requirement 4: When a cross-volume collision occurs, staging file is removed and source remains intact."""
    src = tmp_path / "source" / "photo.jpg"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("Source Photo Data", encoding="utf-8")

    dst = tmp_path / "target" / "photo.jpg"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("Existing Target Photo Data", encoding="utf-8")

    with patch("app.core.resilient_file_ops.IS_WINDOWS", False):
        with patch("os.link", side_effect=OSError(18, "Invalid cross-device link")):
            with pytest.raises(FileExistsError):
                _cross_volume_atomic_move(str(src), str(dst))

    # Existing target and source must be untouched
    assert dst.read_text(encoding="utf-8") == "Existing Target Photo Data"
    assert src.read_text(encoding="utf-8") == "Source Photo Data"
    assert src.exists()

    # Staging files must be cleaned up
    staging_files = [f for f in os.listdir(dst.parent) if f.startswith(".tmp_stage_")]
    assert len(staging_files) == 0


def test_unrecoverable_conflict_logs_warning_and_preserves_source(tmp_path, caplog):
    """Requirement 5: Unrecoverable move conflicts log structured warnings and preserve source files without interrupting batch execution."""
    src = tmp_path / "unmovable.txt"
    src.write_text("Protected Data", encoding="utf-8")
    dst = tmp_path / "dest" / "unmovable.txt"

    # Simulate permission error during atomic move
    with patch(
        "app.core.resilient_file_ops.atomic_move_non_clobber",
        side_effect=PermissionError("Permission denied"),
    ):
        with caplog.at_level(logging.WARNING):
            with pytest.raises(PermissionError):
                resilient_move(str(src), str(dst))

    # Structured warning logged
    assert "Unrecoverable move conflict" in caplog.text
    # Source file preserved intact
    assert src.exists()
    assert src.read_text(encoding="utf-8") == "Protected Data"


def test_batch_execution_resilience_under_move_conflicts(tmp_path):
    """Requirement 5 & Acceptance Criteria: Batch execution proceeds safely without file destruction under move conflicts."""
    from app.core.mover import execute_moves

    base_dir = tmp_path / "base"
    base_dir.mkdir()

    file1 = base_dir / "file1.txt"
    file2 = base_dir / "file2.txt"
    file1.write_text("File 1 content", encoding="utf-8")
    file2.write_text("File 2 content", encoding="utf-8")

    target_dir = base_dir / "Organized"
    target_dir.mkdir()

    # Pre-create file1.txt at target to trigger collision
    (target_dir / "file1.txt").write_text(
        "Pre-existing File 1 at target", encoding="utf-8"
    )

    plan = {
        "Organized": {
            "file1.txt": {
                "node_type": "file",
                "relative_source": "file1.txt",
                "target_filename": "file1.txt",
            },
            "file2.txt": {
                "node_type": "file",
                "relative_source": "file2.txt",
                "target_filename": "file2.txt",
            },
        }
    }

    class DummyDB:
        def get_document(self, base_dir, source_rel_path):
            return None

        def update_document_path(self, base_dir, old_path, new_path):
            pass

        def set_user_verified_target(self, base_dir, file_hash, target):
            pass

    summary = execute_moves(
        base_dir=str(base_dir),
        plan=plan,
        db=DummyDB(),
        history_manager=None,
    )

    # file1.txt collided with pre-existing file1.txt -> moved to file1_1.txt!
    # file2.txt moved to file2.txt!
    assert (target_dir / "file1.txt").read_text(
        encoding="utf-8"
    ) == "Pre-existing File 1 at target"
    assert (target_dir / "file1_1.txt").read_text(encoding="utf-8") == "File 1 content"
    assert (target_dir / "file2.txt").read_text(encoding="utf-8") == "File 2 content"
