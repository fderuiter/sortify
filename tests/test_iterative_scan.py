import os
import shutil
import json
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.config import AppSettings
from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_conn import get_db_connection
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager


@pytest.fixture
def setup_history_env(tmp_path):
    base_dir = str(tmp_path / "test_base")
    os.makedirs(base_dir, exist_ok=True)

    db_worker = DBWorker()
    db_path = tmp_path / "test_docs.db"
    db = Database(db_path, worker=db_worker)

    cache_path = tmp_path / "test_cache.db"
    cache = CacheManager(str(cache_path), worker=db_worker)

    history_manager = HistoryManager(db, cache, str(tmp_path / "test_history.db"))

    yield base_dir, db, history_manager
    db_worker.stop()


def test_deep_nested_scan_no_crash(tmp_path):
    """A file scan completes successfully without crashing when executed against a directory path nested 150 levels deep."""
    curr = tmp_path
    for i in range(150):
        curr = curr / f"level_{i}"
    curr.mkdir(parents=True)
    
    # Put a supported file at the deepest level
    file_path = curr / "deep_file.pdf"
    file_path.write_text("Hello Deep World")
    
    # Scan with a depth limit of 200
    from app.core.scanner import get_files_recursively
    files = get_files_recursively(str(tmp_path), depth_limit=200)
    
    # Should complete without crashing and find the file
    assert len(files) == 1
    parts = files[0].replace("\\", "/").split("/")
    assert len(parts) == 151


def test_directory_traversal_stops_at_limit(tmp_path):
    """Directory traversal stops exactly at the configured depth, omitting files located at deeper levels."""
    # Construct levels
    # level 1: dir_1
    # level 2: dir_1/dir_2
    # level 3: dir_1/dir_2/dir_3
    (tmp_path / "dir_1").mkdir()
    (tmp_path / "dir_1" / "file_1.pdf").write_text("1")
    
    (tmp_path / "dir_1" / "dir_2").mkdir()
    (tmp_path / "dir_1" / "dir_2" / "file_2.pdf").write_text("2")
    
    (tmp_path / "dir_1" / "dir_2" / "dir_3").mkdir()
    (tmp_path / "dir_1" / "dir_2" / "dir_3" / "file_3.pdf").write_text("3")
    
    from app.core.scanner import get_files_recursively
    
    # Depth limit 1: dir_1 is at depth 1. We skip scanning dir_1.
    files_limit_1 = get_files_recursively(str(tmp_path), depth_limit=1)
    assert len(files_limit_1) == 0
    
    # Depth limit 2: dir_1 (depth 1 < 2) is scanned. We get file_1.
    # dir_2 (depth 2 >= 2) is skipped.
    files_limit_2 = get_files_recursively(str(tmp_path), depth_limit=2)
    assert files_limit_2 == [os.path.join("dir_1", "file_1.pdf")]
    
    # Depth limit 3: dir_1 (depth 1 < 3) and dir_2 (depth 2 < 3) are scanned.
    # We get file_1 and file_2. dir_3 (depth 3 >= 3) is skipped.
    files_limit_3 = get_files_recursively(str(tmp_path), depth_limit=3)
    assert sorted(files_limit_3) == sorted([
        os.path.join("dir_1", "file_1.pdf"),
        os.path.join("dir_1", "dir_2", "file_2.pdf"),
    ])


def test_dynamic_configuration_loading(tmp_path):
    """The folder depth limit setting can be updated in the configuration file, is loaded dynamically, and validates inputs."""
    settings_file = tmp_path / "settings.json"
    
    # Write initial settings with a specific FOLDER_DEPTH_LIMIT
    initial_data = {"FOLDER_DEPTH_LIMIT": 42}
    settings_file.write_text(json.dumps(initial_data))
    
    # Load settings
    settings = AppSettings(filepath=str(settings_file))
    assert settings.FOLDER_DEPTH_LIMIT == 42
    
    # Update settings file directly (simulate config file update)
    updated_data = {"FOLDER_DEPTH_LIMIT": 84}
    settings_file.write_text(json.dumps(updated_data))
    
    # Reload settings (simulates dynamic loading/reloading)
    settings.load()
    assert settings.FOLDER_DEPTH_LIMIT == 84
    
    # Discard negative/non-numeric values during runtime assignment
    settings.FOLDER_DEPTH_LIMIT = 50  # valid positive integer
    assert settings.FOLDER_DEPTH_LIMIT == 50
    
    # Verify that setting an invalid value raises a ValidationError
    with pytest.raises(ValidationError):
        settings.FOLDER_DEPTH_LIMIT = -10
        
    with pytest.raises(ValidationError):
        settings.FOLDER_DEPTH_LIMIT = "not-a-number"
        
    with pytest.raises(ValidationError):
        settings.FOLDER_DEPTH_LIMIT = True  # boolean is discarded/rejected


def test_alphabetical_deterministic_sorting(tmp_path):
    """Scanned files are returned in a sorted, deterministic order matching alphabetical directory lists."""
    # Create subdirectories and files in non-alphabetical order
    (tmp_path / "b_dir").mkdir()
    (tmp_path / "a_dir").mkdir()
    (tmp_path / "b_dir" / "y_file.pdf").write_text("y")
    (tmp_path / "b_dir" / "x_file.pdf").write_text("x")
    (tmp_path / "a_dir" / "z_file.pdf").write_text("z")
    
    from app.core.scanner import get_files_recursively
    
    files = get_files_recursively(str(tmp_path), depth_limit=10)
    
    expected = sorted([
        os.path.join("a_dir", "z_file.pdf"),
        os.path.join("b_dir", "x_file.pdf"),
        os.path.join("b_dir", "y_file.pdf"),
    ])
    assert files == expected


def test_history_and_rollback_with_depth_limits(setup_history_env):
    """History snapshots and rollback operations run successfully following a deep scan, even when files are skipped due to the depth limit."""
    base_dir, db, history_manager = setup_history_env
    
    # Create a 5-level directory structure with files at each level
    curr = base_dir
    paths = []
    for i in range(5):
        curr_dir = os.path.join(curr, f"dir_{i}")
        os.makedirs(curr_dir, exist_ok=True)
        file_path = os.path.join(curr_dir, f"file_{i}.pdf")
        with open(file_path, "w") as f:
            f.write(f"content_{i}")
        db.upsert_document(base_dir, os.path.relpath(file_path, base_dir), f"hash_{i}", f"text_{i}")
        paths.append(file_path)
        curr = curr_dir

    # We will patch FOLDER_DEPTH_LIMIT to be 3
    # This means level 3 and 4 are skipped by the scanner
    with patch("app.config.AppSettings.FOLDER_DEPTH_LIMIT", new=3, create=True):
        # Create snapshot under depth limit 3
        session_id = history_manager.create_snapshot(base_dir)
        
        # Check missing files - should succeed with no missing files reported
        missing = history_manager.check_missing_files(session_id)
        assert len(missing) == 0
        
        # Now change depth limit to 2
        # Under limit 2, dir_2 is not scanned. So file_2 is skipped.
        # But file_2 is in the snapshot (since snapshot was taken with limit 3).
        # Our direct disk check fallback should find file_2 on disk, and NOT mark it as missing!
        with patch("app.config.AppSettings.FOLDER_DEPTH_LIMIT", new=2, create=True):
            missing_under_2 = history_manager.check_missing_files(session_id)
            assert len(missing_under_2) == 0
            
            # Perform a rollback under limit 2 - should execute smoothly!
            history_manager.rollback(session_id)
