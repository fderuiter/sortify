import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager
from app.core.link_manager import LinkManager
from app.core.mover import execute_moves, _create_junction, resolve_new_target
from app.core.path_utils import is_junction_path, is_junction_entry
from app.core.resilient_file_ops import resilient_remove, resilient_rmtree
from app.core.scanner import get_files_recursively


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


def test_is_junction_utilities(tmp_path):
    target = tmp_path / "target_folder"
    target.mkdir()
    junc = tmp_path / "junc_folder"
    _create_junction(str(target), str(junc))

    # On non-Windows, _create_junction creates a directory symlink.
    # We patch os.path.isjunction to simulate junction behavior if running on Linux.
    with patch("os.path.isjunction", side_effect=lambda p: str(p) == str(junc) or (sys.platform == "win32" and os.path.isjunction(p))):
        assert is_junction_path(str(junc))
        assert not is_junction_path(str(target))

        mock_entry = MagicMock()
        mock_entry.is_junction.return_value = True
        assert is_junction_entry(mock_entry)


def test_scanner_detects_junction_without_recursing(tmp_path):
    base_dir = str(tmp_path)
    target_dir = os.path.join(base_dir, "target_dir")
    os.makedirs(target_dir, exist_ok=True)

    file1 = os.path.join(target_dir, "doc1.txt")
    with open(file1, "w") as f:
        f.write("content 1")

    junc_path = os.path.join(base_dir, "junc_dir")
    _create_junction(target_dir, junc_path)

    # Patch os.path.isjunction & is_junction_entry to ensure junc_dir is treated as junction on all platforms
    def mock_isjunction(path):
        return os.path.abspath(path) == os.path.abspath(junc_path)

    with patch("app.core.scanner.is_junction_entry", side_effect=lambda e: getattr(e, "name", "") == "junc_dir"), \
         patch("app.core.link_manager.is_junction_path", side_effect=mock_isjunction):

        files = get_files_recursively(base_dir)

        # Scanner should find target_dir/doc1.txt and junc_dir (link entry), but NOT junc_dir/doc1.txt
        assert "junc_dir" in files
        assert "target_dir/doc1.txt" in [f.replace("\\", "/") for f in files]
        assert "junc_dir/doc1.txt" not in [f.replace("\\", "/") for f in files]

        info = LinkManager.get_link_info(os.path.abspath(junc_path))
        assert info is not None
        assert info["type"] == "junction"
        assert os.path.normpath(info["target"]) == os.path.normpath(target_dir)


def test_relocate_target_directory_updates_junction(tmp_path):
    base_dir = str(tmp_path)
    target_dir = os.path.join(base_dir, "target_dir")
    os.makedirs(target_dir, exist_ok=True)

    target_file = os.path.join(target_dir, "data.txt")
    with open(target_file, "w") as f:
        f.write("junction data")

    junc_path = os.path.join(base_dir, "junc_dir")
    _create_junction(target_dir, junc_path)

    def mock_isjunction(path):
        p = os.path.abspath(path)
        return p in (os.path.abspath(junc_path), os.path.abspath(os.path.join(base_dir, "sorted", "target_dir")))

    with patch("app.core.scanner.is_junction_entry", side_effect=lambda e: getattr(e, "name", "") == "junc_dir"), \
         patch("app.core.link_manager.is_junction_path", side_effect=mock_isjunction), \
         patch("app.core.mover.is_junction_path", side_effect=mock_isjunction):

        get_files_recursively(base_dir)

        plan = {
            "sorted": {
                "target_dir": {
                    "data.txt": {
                        "__type__": "file",
                        "relative_source": "../../target_dir/data.txt",
                        "status": "Pending Move",
                        "source_path": "target_dir/data.txt",
                        "target_filename": "data.txt",
                    }
                }
            },
            "junc_dir": {
                "__type__": "file",
                "relative_source": "junc_dir",
            }
        }

        execute_moves(base_dir, plan, db, history_manager)

        new_target_dir = os.path.join(base_dir, "sorted", "target_dir")
        assert os.path.exists(os.path.join(new_target_dir, "data.txt"))

        # Check junction was updated to point to new_target_dir
        if sys.platform == "win32":
            read_target = os.readlink(junc_path)
            assert os.path.normpath(read_target) == os.path.normpath(new_target_dir)


def test_directory_cleanup_deletes_junction_without_modifying_target_files(tmp_path):
    base_dir = str(tmp_path)
    target_dir = os.path.join(base_dir, "target_dir")
    os.makedirs(target_dir, exist_ok=True)

    target_file = os.path.join(target_dir, "important.txt")
    with open(target_file, "w") as f:
        f.write("DO NOT DELETE")

    cleanup_folder = os.path.join(base_dir, "folder_to_clean")
    os.makedirs(cleanup_folder, exist_ok=True)

    junc_path = os.path.join(cleanup_folder, "target_link")
    _create_junction(target_dir, junc_path)

    def mock_isjunction(path):
        return os.path.abspath(path) == os.path.abspath(junc_path)

    with patch("app.core.mover.is_junction_path", side_effect=mock_isjunction), \
         patch("app.core.resilient_file_ops.is_junction_path", side_effect=mock_isjunction):

        # Delete the junction directly
        resilient_remove(junc_path)

        assert not os.path.lexists(junc_path)
        # Verify target directory and files are completely intact!
        assert os.path.exists(target_dir)
        assert os.path.exists(target_file)
        with open(target_file, "r") as f:
            assert f.read() == "DO NOT DELETE"


def test_resilient_rmtree_on_junction_removes_link_only(tmp_path):
    base_dir = str(tmp_path)
    target_dir = os.path.join(base_dir, "real_target")
    os.makedirs(target_dir, exist_ok=True)

    target_file = os.path.join(target_dir, "keep_me.txt")
    with open(target_file, "w") as f:
        f.write("safe")

    junc_path = os.path.join(base_dir, "junc_point")
    _create_junction(target_dir, junc_path)

    def mock_isjunction(path):
        return os.path.abspath(path) == os.path.abspath(junc_path)

    with patch("app.core.resilient_file_ops.is_junction_path", side_effect=mock_isjunction):
        resilient_rmtree(junc_path)

        assert not os.path.lexists(junc_path)
        assert os.path.exists(target_file)


def test_resolve_new_target_logic():
    path_map = {
        os.path.normcase(os.path.abspath("/base/target_dir/file1.txt")): os.path.abspath("/base/sorted/target_dir/file1.txt")
    }

    resolved = resolve_new_target("/base/target_dir", path_map)
    assert os.path.normpath(resolved) == os.path.normpath("/base/sorted/target_dir")
