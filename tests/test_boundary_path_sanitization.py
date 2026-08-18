import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from app.core.path_utils import sanitize_folder_key, sanitize_plan
from app.core.verifier import VerificationEngine


def test_sanitize_folder_key_illegal_chars_and_null_bytes():
    """Verify illegal filesystem characters, colons, slashes, and null bytes are sanitized."""
    key = "Folder: 1/Type\x00Sub?Name"
    safe_key, transformed = sanitize_folder_key(key)
    assert transformed is True
    assert ":" not in safe_key
    assert "/" not in safe_key
    assert "\x00" not in safe_key
    assert "?" not in safe_key
    assert safe_key == "Folder_ 1_TypeSub_Name"


def test_sanitize_folder_key_os_reserved_keywords():
    """Verify OS reserved names are mapped to safe variants."""
    reserved_keys = ["CON", "prn", "AUX", "NUL", "COM1", "LPT9"]
    for key in reserved_keys:
        safe_key, transformed = sanitize_folder_key(key)
        assert transformed is True
        assert safe_key.lower().endswith("_safe")


def test_sanitize_folder_key_path_traversal():
    """Verify directory traversal segments like .. and . are stripped."""
    key = "../traversal_dir/sub"
    safe_key, transformed = sanitize_folder_key(key)
    assert transformed is True
    assert ".." not in safe_key
    assert safe_key == "traversal_dir_sub"


def test_verify_plan_integrity_sanitizes_keys_and_generates_warnings():
    """Verify VerificationEngine sanitizes invalid keys and returns warnings."""
    base_dir = "/base/dir"
    plan = {
        "Data: Archives": {
            "file1.txt": {
                "__type__": "file",
                "relative_source": "file1.txt",
                "target_filename": "file1.txt",
            }
        },
        "CON": {
            "file2.txt": {
                "__type__": "file",
                "relative_source": "file2.txt",
                "target_filename": "file2.txt",
            }
        },
    }

    result = VerificationEngine.verify_plan_integrity(base_dir, plan)

    assert result["success"] is True
    assert "Data_ Archives" in plan
    assert "CON_safe" in plan
    assert "Data: Archives" not in plan
    assert "CON" not in plan

    warnings = result["warnings"]
    assert any("Data: Archives" in w and "Data_ Archives" in w for w in warnings)
    assert any("CON" in w and "CON_safe" in w for w in warnings)


def test_duplicate_target_folder_merging():
    """Verify folder keys that sanitize to the same key are merged without errors (Requirement 5)."""
    base_dir = "/base/dir"
    plan = {
        "Data: Archives": {
            "file1.txt": {
                "__type__": "file",
                "relative_source": "file1.txt",
                "target_filename": "file1.txt",
            }
        },
        "Data? Archives": {
            "file2.txt": {
                "__type__": "file",
                "relative_source": "file2.txt",
                "target_filename": "file2.txt",
            }
        },
    }

    result = VerificationEngine.verify_plan_integrity(base_dir, plan)

    assert result["success"] is True
    assert "Data_ Archives" in plan
    assert len(plan) == 1
    # Both files must be present under the merged target folder
    assert "file1.txt" in plan["Data_ Archives"]
    assert "file2.txt" in plan["Data_ Archives"]


def test_duplicate_file_disambiguation_during_merge():
    """Verify duplicate filenames in merged folders are disambiguated safely."""
    base_dir = "/base/dir"
    plan = {
        "Folder: A": {
            "doc.pdf": {
                "__type__": "file",
                "relative_source": "sub1/doc.pdf",
                "target_filename": "doc.pdf",
            }
        },
        "Folder? A": {
            "doc.pdf": {
                "__type__": "file",
                "relative_source": "sub2/doc.pdf",
                "target_filename": "doc.pdf",
            }
        },
    }

    result = VerificationEngine.verify_plan_integrity(base_dir, plan)

    assert result["success"] is True
    assert "Folder_ A" in plan
    merged_folder = plan["Folder_ A"]
    assert len(merged_folder) == 2
    assert "doc.pdf" in merged_folder
    assert "doc_1.pdf" in merged_folder


def test_batch_moves_execute_without_invalid_path_errors():
    """Verify real file move operations complete successfully with sanitized keys."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Create test source files
        f1 = os.path.join(tmp_dir, "file1.txt")
        f2 = os.path.join(tmp_dir, "file2.txt")
        with open(f1, "w") as f:
            f.write("content 1")
        with open(f2, "w") as f:
            f.write("content 2")

        plan = {
            "Invoices: 2023": {
                "file1.txt": {
                    "__type__": "file",
                    "relative_source": "../file1.txt",
                    "target_filename": "file1.txt",
                }
            },
            "AUX": {
                "file2.txt": {
                    "__type__": "file",
                    "relative_source": "../file2.txt",
                    "target_filename": "file2.txt",
                }
            },
        }

        from app.config import AppSettings
        from app.core.history import HistoryManager
        from app.core.mover import execute_moves

        settings = AppSettings()
        history_mgr = MagicMock(spec=HistoryManager)
        history_mgr.create_snapshot.return_value = "test_session"

        # Verify plan first to sanitize keys
        result = VerificationEngine.verify_plan_integrity(tmp_dir, plan)
        assert result["success"] is True

        mock_db = MagicMock()
        summary = execute_moves(tmp_dir, plan, db=mock_db, history_manager=history_mgr, runtime_settings=settings)

        # Confirm target folders were created with safe names and files moved
        target_inv = os.path.join(tmp_dir, "Invoices_ 2023", "file1.txt")
        target_aux = os.path.join(tmp_dir, "AUX_safe", "file2.txt")

        assert os.path.exists(target_inv)
        assert os.path.exists(target_aux)


@pytest.mark.anyio
async def test_ui_verify_current_plan_displays_sanitization_warnings():
    """Verify the UI preview displays path modification warnings before execution."""
    from app.ui.app import AutoSorterApp

    app = AutoSorterApp.__new__(AutoSorterApp)
    app.base_dir = "/base/dir"
    app.plan = {
        "Data: Archives": {
            "doc.txt": {
                "__type__": "file",
                "relative_source": "doc.txt",
                "target_filename": "doc.txt",
            }
        }
    }
    app.warnings_label = MagicMock()
    app.update_ai_warning = MagicMock()

    await app.verify_current_plan()

    assert app.plan == {
        "Data_ Archives": {
            "doc.txt": {
                "__type__": "file",
                "relative_source": "doc.txt",
                "target_filename": "doc.txt",
            }
        }
    }
    app.warnings_label.set_visibility.assert_called_with(True)
    warn_text = app.warnings_label.set_text.call_args[0][0]
    assert "Data: Archives" in warn_text
    assert "Data_ Archives" in warn_text
