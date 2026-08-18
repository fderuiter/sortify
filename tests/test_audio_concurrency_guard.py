"""Tests for Audio Extractor Concurrency Semaphore Guard and AUDIO_MAX_WORKERS configuration.
"""

import concurrent.futures
import time
from unittest.mock import MagicMock, patch
import pytest
from pydantic import ValidationError

from app.config import AppSettings, Settings
from app.core.extractor import extract_file_text, build_corpus_generator
from app.core.extractor_strategies import AudioExtractor
from app.core.shared_registry import AudioConcurrencyGuard


@pytest.fixture(autouse=True)
def reset_guard():
    """Reset singleton guard state before and after each test."""
    AudioConcurrencyGuard.reset_instance()
    yield
    AudioConcurrencyGuard.reset_instance()


def test_audio_max_workers_setting_defaults_and_validation():
    """Test default values, bounds, and aliases for AUDIO_MAX_WORKERS."""
    s = Settings()
    assert s.AUDIO_MAX_WORKERS == 2
    assert s.MAX_AUDIO_WORKERS == 2

    # Test setting via property alias
    s.MAX_AUDIO_WORKERS = 4
    assert s.AUDIO_MAX_WORKERS == 4
    assert s.MAX_AUDIO_WORKERS == 4

    # Test bounds: 1 to 64 allowed
    s_valid = Settings(AUDIO_MAX_WORKERS=1)
    assert s_valid.AUDIO_MAX_WORKERS == 1
    s_valid64 = Settings(AUDIO_MAX_WORKERS=64)
    assert s_valid64.AUDIO_MAX_WORKERS == 64

    # Test invalid values trigger ValidationError
    with pytest.raises(ValidationError):
        Settings(AUDIO_MAX_WORKERS=0)

    with pytest.raises(ValidationError):
        Settings(AUDIO_MAX_WORKERS=65)

    with pytest.raises(ValidationError):
        Settings(AUDIO_MAX_WORKERS=-5)


def test_app_settings_audio_max_workers(tmp_path):
    """Test AppSettings dynamic getter/setter and persistence for AUDIO_MAX_WORKERS."""
    settings_file = tmp_path / "settings.json"
    app_settings = AppSettings(filepath=str(settings_file))

    assert app_settings.AUDIO_MAX_WORKERS == 2
    assert app_settings.MAX_AUDIO_WORKERS == 2

    app_settings.AUDIO_MAX_WORKERS = 3
    assert app_settings.AUDIO_MAX_WORKERS == 3
    assert app_settings.MAX_AUDIO_WORKERS == 3

    app_settings.MAX_AUDIO_WORKERS = 5
    assert app_settings.AUDIO_MAX_WORKERS == 5
    assert app_settings.MAX_AUDIO_WORKERS == 5


def test_audio_concurrency_guard_singleton_and_limit():
    """Test AudioConcurrencyGuard singleton instance, limit setting, and active count."""
    guard = AudioConcurrencyGuard.get_instance(limit=2)
    assert guard.limit == 2
    assert guard.active_count == 0

    # Updating limit via get_instance
    guard2 = AudioConcurrencyGuard.get_instance(limit=4)
    assert guard2 is guard
    assert guard.limit == 4

    # Direct set_limit
    guard.set_limit(3)
    assert guard.limit == 3


def test_audio_concurrency_guard_restricts_active_tasks():
    """Test that AudioConcurrencyGuard restricts active concurrency to configured limit."""
    guard = AudioConcurrencyGuard.get_instance(limit=2)
    active_records = []
    peak_active = 0
    import threading
    lock = threading.Lock()

    def simulate_audio_task(task_id: int):
        nonlocal peak_active
        cancel_check = lambda: False
        with guard.guard(cancel_check=cancel_check) as acquired:
            assert acquired is True
            with lock:
                curr = guard.active_count
                active_records.append(curr)
                if curr > peak_active:
                    peak_active = curr
            time.sleep(0.05)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(simulate_audio_task, i) for i in range(10)]
        for f in futures:
            f.result()

    assert peak_active <= 2
    assert guard.active_count == 0


def test_audio_extraction_batch_concurrency_limit(tmp_path):
    """Test that processing a batch with more audio files than limit restricts active transcriptions."""
    # Create dummy audio wav files
    audio_files = []
    for i in range(6):
        p = tmp_path / f"audio_{i}.wav"
        p.write_bytes(b"")  # 0-byte compliant wav test dummy
        audio_files.append(str(p))

    guard = AudioConcurrencyGuard.get_instance(limit=2)
    peak_active = 0
    import threading
    lock = threading.Lock()

    def mock_do_extract(file_path, settings=None, progress_callback=None, cancel_check=None):
        nonlocal peak_active
        with lock:
            curr = guard.active_count
            if curr > peak_active:
                peak_active = curr
        time.sleep(0.05)
        return "Transcribed audio text payload"

    mock_settings = MagicMock()
    mock_settings.AUDIO_MAX_WORKERS = 2
    mock_settings.AUDIO_GPU_ENABLED = False

    extractor = AudioExtractor()

    with patch.object(extractor, "_do_extract", side_effect=mock_do_extract):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = [
                pool.submit(extractor.extract, f, settings=mock_settings)
                for f in audio_files
            ]
            results = [f.result() for f in futures]

    assert peak_active <= 2
    assert all("Transcribed audio text payload" in r for r in results)
    assert guard.active_count == 0


def test_non_audio_files_not_blocked_by_audio_guard(tmp_path):
    """Test that non-audio extractions complete immediately without waiting for audio guard slots."""
    guard = AudioConcurrencyGuard.get_instance(limit=1)

    # Acquire the single audio guard slot manually
    guard.acquire_slot()
    assert guard.active_count == 1

    try:
        # Create a text file
        txt_path = tmp_path / "sample.txt"
        txt_path.write_text("Hello world text file", encoding="utf-8")

        start_time = time.time()
        # Extract text file while audio guard slot is fully saturated
        res = extract_file_text(str(txt_path))
        elapsed = time.time() - start_time

        assert res == "Hello world text file"
        # Must complete immediately (< 0.5s) without waiting on audio guard
        assert elapsed < 0.5
    finally:
        guard.release_slot()


def test_cancellation_while_waiting_for_guard(tmp_path):
    """Test that cancellation requests terminate tasks waiting for audio guard without deadlock."""
    guard = AudioConcurrencyGuard.get_instance(limit=1)

    wav_file = tmp_path / "test.wav"
    wav_file.write_bytes(b"")

    # Fill the guard slot
    guard.acquire_slot()

    cancelled = False

    def cancel_check():
        return cancelled

    def worker():
        extractor = AudioExtractor()
        return extractor.extract(str(wav_file), cancel_check=cancel_check)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(worker)
        time.sleep(0.05)  # Ensure worker is waiting at guard
        cancelled = True
        res = fut.result(timeout=2.0)

    assert res == "[STATUS:CANCELLED]"
    guard.release_slot()


def test_cancellation_pre_flagged(tmp_path):
    """Test that if cancel_check returns True initially, extract returns CANCELLED immediately."""
    guard = AudioConcurrencyGuard.get_instance(limit=2)
    extractor = AudioExtractor()

    wav_file = tmp_path / "test.wav"
    wav_file.write_bytes(b"")

    res = extractor.extract(str(wav_file), cancel_check=lambda: True)
    assert res == "[STATUS:CANCELLED]"
    assert guard.active_count == 0


def test_dynamic_audio_max_workers_update():
    """Test that updating AUDIO_MAX_WORKERS expands guard capacity dynamically."""
    guard = AudioConcurrencyGuard.get_instance(limit=1)

    # Acquire 1 slot
    guard.acquire_slot()
    assert guard.active_count == 1

    # Expanding limit to 3
    guard.set_limit(3)
    assert guard.limit == 3

    # Now we should be able to acquire a 2nd slot immediately
    acquired = guard.acquire_slot()
    assert acquired is True
    assert guard.active_count == 2

    guard.release_slot()
    guard.release_slot()
    assert guard.active_count == 0


def test_exception_safety_in_audio_extractor():
    """Test that if an exception occurs during audio extraction, the guard slot is released."""
    guard = AudioConcurrencyGuard.get_instance(limit=1)
    extractor = AudioExtractor()

    def mock_do_extract_with_error(*args, **kwargs):
        raise RuntimeError("Simulated transcription crash")

    with patch.object(extractor, "_do_extract", side_effect=mock_do_extract_with_error):
        with pytest.raises(RuntimeError, match="Simulated transcription crash"):
            extractor.extract("test.wav")

    # Guard slot must have been released by context manager
    assert guard.active_count == 0
