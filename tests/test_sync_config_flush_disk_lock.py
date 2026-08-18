import concurrent.futures
import json

import pytest
from pydantic import ValidationError

from app.config import AppSettings, _flush_all_on_exit


def test_flush_before_reload_preserves_new_setting(tmp_path):
    """Updating a setting immediately before a reload should flush pending edits to disk."""
    mock_filepath = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(mock_filepath))

    settings.MAX_WORKERS = 8
    # Timer is scheduled, verify flush() executes save synchronously
    assert settings._save_timer is not None

    # Simulate background process reload
    settings.flush()
    assert settings._save_timer is None

    # Verify disk content contains updated setting
    with open(mock_filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["MAX_WORKERS"] == 8

    # Reload from disk
    settings.load()
    assert settings.MAX_WORKERS == 8


def test_multi_instance_cross_module_flush(tmp_path):
    """Loading settings on a second instance flushes pending saves from the first instance."""
    mock_filepath = tmp_path / "settings.json"
    settings_ui = AppSettings(filepath=str(mock_filepath))
    settings_worker = AppSettings(filepath=str(mock_filepath))

    # UI modifies a setting
    settings_ui.MAX_DEPTH = 7
    assert settings_ui._save_timer is not None

    # Worker triggers load()
    settings_worker.load()

    # Worker should read the newly saved value
    assert settings_worker.MAX_DEPTH == 7

    if settings_ui._save_timer:
        settings_ui._save_timer.cancel()
    if settings_worker._save_timer:
        settings_worker._save_timer.cancel()


def test_simultaneous_reads_and_writes_under_disk_lock(tmp_path):
    """Concurrent reads and writes across threads/instances complete without format or access errors."""
    mock_filepath = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(mock_filepath))

    errors = []

    def writer_task(worker_id):
        try:
            for i in range(10):
                val = (worker_id * 10 + i) % 50 + 1
                settings.MAX_FOLDERS = val
                settings.flush()
        except Exception as e:
            errors.append(e)

    def reader_task():
        try:
            for _ in range(10):
                inst = AppSettings(filepath=str(mock_filepath))
                inst.load()
                _ = inst.MAX_FOLDERS
        except Exception as e:
            errors.append(e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = []
        for worker_id in range(4):
            futures.append(executor.submit(writer_task, worker_id))
            futures.append(executor.submit(reader_task))
        concurrent.futures.wait(futures)

    assert not errors, f"Concurrent read/write encountered errors: {errors}"


def test_atexit_flush_on_shutdown(tmp_path):
    """Exit handler flushes pending settings saves prior to termination."""
    mock_filepath = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(mock_filepath))

    settings.MAX_WORKERS = 16
    assert settings._save_timer is not None

    # Simulate process shutdown exit hook
    _flush_all_on_exit()

    assert settings._save_timer is None
    with open(mock_filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["MAX_WORKERS"] == 16


def test_invalid_setting_assignment_raises_validation_error_and_skips_save(tmp_path):
    """Invalid settings inputs generate immediate validation errors without scheduling saves."""
    mock_filepath = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(mock_filepath))

    if settings._save_timer:
        settings._save_timer.cancel()
        settings._save_timer = None

    initial_workers = settings.MAX_WORKERS

    with pytest.raises(ValidationError):
        settings.MAX_WORKERS = 999  # Out of bounds (>64)

    assert settings.MAX_WORKERS == initial_workers
    assert settings._save_timer is None


def test_reload_corrupted_file_retains_valid_in_memory_settings(tmp_path):
    """Attempting to reload a corrupted configuration file retains current valid in-memory settings."""
    mock_filepath = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(mock_filepath))

    # Set valid in-memory configuration
    settings.MAX_WORKERS = 10
    settings.flush()

    # Corrupt configuration file on disk
    mock_filepath.write_text("{ corrupt json data ...")

    # Reload configuration
    settings.load()

    # Valid in-memory setting must be preserved
    assert settings.MAX_WORKERS == 10
    assert settings._has_validation_errors is True
