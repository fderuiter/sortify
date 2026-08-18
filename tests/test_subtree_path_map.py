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


def test_subtree_directory_expansion(test_env):
    """
    Requirement 1 & Acceptance Criterion 1:
    Directory move plans explicitly map all nested files and subfolders prior to execution.
    """
    base_dir, db, history_manager = test_env

    # Create folder structure with nested files and subdirectories
    folder_a = os.path.join(base_dir, "folder_a")
    sub1 = os.path.join(folder_a, "sub1")
    os.makedirs(sub1, exist_ok=True)

    file1 = os.path.join(sub1, "file1.txt")
    file2 = os.path.join(sub1, "file2.txt")
    file3 = os.path.join(folder_a, "file3.txt")

    for fpath in [file1, file2, file3]:
        with open(fpath, "w") as f:
            f.write("content")

    plan = {
        "TargetFolder": {
            "folder_a": {
                "__type__": "directory",
                "relative_source": "folder_a",
            }
        }
    }

    moves = VerificationEngine.get_moves(base_dir, plan)

    # Check that all nested files and subdirectories are explicitly mapped
    sources = {os.path.normpath(src) for _, src, _ in moves}
    assert os.path.normpath(folder_a) in sources
    assert os.path.normpath(sub1) in sources
    assert os.path.normpath(file1) in sources
    assert os.path.normpath(file2) in sources
    assert os.path.normpath(file3) in sources

    # Check destination paths match expanded structure
    dest_map = {os.path.normpath(src): os.path.normpath(dst) for _, src, dst in moves}
    expected_target_file1 = os.path.join(base_dir, "TargetFolder", "folder_a", "sub1", "file1.txt")
    assert dest_map[os.path.normpath(file1)] == os.path.normpath(expected_target_file1)


def test_preflight_verification_flags_broken_relative_links_in_subtree(test_env):
    """
    Requirement 2 & Acceptance Criterion 2:
    Pre-flight verification flags broken relative links contained within relocated subtrees.
    """
    base_dir, db, history_manager = test_env

    folder_a = os.path.join(base_dir, "folder_a")
    sub = os.path.join(folder_a, "sub")
    os.makedirs(sub, exist_ok=True)

    data_file = os.path.join(sub, "data.txt")
    with open(data_file, "w") as f:
        f.write("data")

    # Create good link and broken link
    good_link = os.path.join(sub, "good.lnk")
    broken_link = os.path.join(sub, "broken.lnk")

    with open(good_link, "w") as f:
        f.write("data.txt")

    with open(broken_link, "w") as f:
        f.write("nonexistent_file.txt")

    plan = {
        "TargetFolder": {
            "folder_a": {
                "__type__": "directory",
                "relative_source": "folder_a",
            }
        }
    }

    result = VerificationEngine.verify_plan_integrity(base_dir, plan)

    assert result["success"] is False
    assert len(result["broken_links"]) > 0
    broken_paths = [os.path.normpath(b["path"]) for b in result["broken_links"]]
    assert any("broken.lnk" in p for p in broken_paths)


def test_relative_symlink_recomputation_and_atomic_replace(test_env):
    """
    Requirement 3, 4 & Acceptance Criteria 3, 4:
    Relative symlink target offsets are correctly recomputed and updated at their final destination paths
    using atomic shadow file replacement.
    """
    if os.name == "nt":
        pytest.skip("Symlink creation requires administrative privileges on Windows")

    base_dir, db, history_manager = test_env

    src_dir = os.path.join(base_dir, "src_dir")
    sub = os.path.join(src_dir, "sub")
    os.makedirs(sub, exist_ok=True)

    target_file = os.path.join(sub, "target.txt")
    with open(target_file, "w") as f:
        f.write("payload")

    # Symlink pointing to target.txt from inside sub
    symlink_file = os.path.join(sub, "link.txt")
    os.symlink("target.txt", symlink_file)

    # Symlink pointing to sub/target.txt from src_dir root
    outer_symlink = os.path.join(src_dir, "outer_link.txt")
    os.symlink("sub/target.txt", outer_symlink)

    plan = {
        "relocated": {
            "src_dir": {
                "__type__": "directory",
                "relative_source": "src_dir",
            }
        }
    }

    result = VerificationEngine.verify_plan_integrity(base_dir, plan)
    assert result["success"] is True

    execute_moves(base_dir, plan, db, history_manager)

    new_sub = os.path.join(base_dir, "relocated", "src_dir", "sub")
    new_symlink = os.path.join(new_sub, "link.txt")
    new_outer = os.path.join(base_dir, "relocated", "src_dir", "outer_link.txt")

    assert os.path.islink(new_symlink)
    assert os.path.islink(new_outer)

    # Recomputed targets should resolve and be functional
    assert os.path.exists(new_symlink)
    assert os.path.exists(new_outer)
    with open(new_symlink, "r") as f:
        assert f.read() == "payload"

    # Ensure no shadow files left behind
    for root, _, files in os.walk(base_dir):
        for f in files:
            assert ".shadow_" not in f


def test_nonexistent_or_unresolvable_shortcut_fails_preflight(test_env):
    """
    Requirement 5 & Acceptance Criterion 5:
    Non-existent or unresolvable shortcut targets fail pre-flight validation before any disk operations begin.
    """
    base_dir, db, history_manager = test_env

    src_folder = os.path.join(base_dir, "src_folder")
    os.makedirs(src_folder, exist_ok=True)

    bad_shortcut = os.path.join(src_folder, "invalid.lnk")
    with open(bad_shortcut, "w") as f:
        f.write("D:\\NonExistentDrive\\missing.exe")

    plan = {
        "dest": {
            "src_folder": {
                "__type__": "directory",
                "relative_source": "src_folder",
            }
        }
    }

    result = VerificationEngine.verify_plan_integrity(base_dir, plan)

    assert result["success"] is False
    assert len(result["broken_links"]) > 0
    # Physical file move should NOT have executed
    assert os.path.exists(bad_shortcut)
