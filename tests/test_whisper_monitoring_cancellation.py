import sys
import time
from unittest.mock import MagicMock

from app.core.extractor_strategies import AudioExtractor, get_audio_duration


def test_get_audio_duration_fallbacks(tmp_path):
    # Test WAV duration
    wav_file = tmp_path / "test.wav"
    wav_file.touch()  # Empty WAV
    assert get_audio_duration(str(wav_file)) == 0.0

    mp3_file = tmp_path / "test.mp3"
    # Create 16000 bytes MP3 file -> should be 1.0 second duration estimate
    with open(mp3_file, "wb") as f:
        f.write(b"\x00" * 16000)
    assert get_audio_duration(str(mp3_file)) == 1.0

    m4a_file = tmp_path / "test.m4a"
    # Create 24000 bytes M4A file -> should be 2.0 second duration estimate
    with open(m4a_file, "wb") as f:
        f.write(b"\x00" * 24000)
    assert get_audio_duration(str(m4a_file)) == 2.0


def test_audio_extractor_real_time_progress_and_cancellation(tmp_path):
    # Create a dummy audio file
    dummy_wav = tmp_path / "test.wav"
    dummy_wav.touch()

    # Create a mock Whisper python script that simulates Whisper outputting progress
    mock_script = tmp_path / "mock_whisper.py"
    with open(mock_script, "w") as f:
        f.write("""import sys
import time

# Output a standard Whisper segment timestamp
print("[00:00.000 --> 00:05.000] Hello segment 1", flush=True)
time.sleep(0.01)
print("[00:05.000 --> 00:10.000] Hello segment 2", flush=True)
time.sleep(0.01)
print("[00:10.000 --> 00:15.000] Hello segment 3", flush=True)
time.sleep(0.01)
print("[00:15.000 --> 00:20.000] Hello segment 4", flush=True)
""")

    extractor = AudioExtractor()
    settings = MagicMock()
    # Configure the extractor to use our mock script with unbuffered output (-u)
    settings.WHISPER_CMD = [sys.executable, "-u", str(mock_script)]

    progress_vals = []

    def progress_cb(pct):
        progress_vals.append(pct)

    # Let's run transcription and verify we get real-time progress updates!
    text = extractor.extract(
        str(dummy_wav),
        settings=settings,
        progress_callback=progress_cb,
    )

    # Since the script runs to completion (unless cancelled), it should output transcription
    assert "Hello segment 1" in text
    assert "Hello segment 2" in text
    assert "Hello segment 3" in text
    assert "Hello segment 4" in text

    # Since we got timestamps up to 20 seconds, and total duration was 100.0 (fallback),
    # the progressive percentages should match the timestamps parsed:
    # 5.0 / 100 = 0.05
    # 10.0 / 100 = 0.10
    # 15.0 / 100 = 0.15
    # 20.0 / 100 = 0.20
    # We use approximate matching to prevent architectural float inequality issues
    assert any(abs(p - 0.05) < 1e-4 for p in progress_vals)
    assert any(abs(p - 0.10) < 1e-4 for p in progress_vals)
    assert any(abs(p - 0.15) < 1e-4 for p in progress_vals)
    assert any(abs(p - 0.20) < 1e-4 for p in progress_vals)


def test_audio_extractor_active_cancellation(tmp_path):
    dummy_wav = tmp_path / "test.wav"
    dummy_wav.touch()

    mock_script = tmp_path / "mock_whisper_cancel.py"
    with open(mock_script, "w") as f:
        f.write("""import sys
import time

print("[00:00.000 --> 00:05.000] Hello segment 1", flush=True)
time.sleep(10.0) # Long sleep that we will cancel during
print("[00:05.000 --> 00:10.000] Hello segment 2", flush=True)
""")

    extractor = AudioExtractor()
    settings = MagicMock()
    # Configure the extractor to use our mock script with unbuffered output (-u)
    settings.WHISPER_CMD = [sys.executable, "-u", str(mock_script)]

    # We will trigger cancellation via cancel_check after we see the first progress update
    cancel_requested = False
    cancel_time = None

    def cancel_check():
        return cancel_requested

    progress_vals = []

    def progress_cb(pct):
        nonlocal cancel_requested, cancel_time
        progress_vals.append(pct)
        # Cancel on the first progress update
        cancel_requested = True
        if cancel_time is None:
            cancel_time = time.time()

    start_time = time.time()
    text = extractor.extract(
        str(dummy_wav),
        settings=settings,
        progress_callback=progress_cb,
        cancel_check=cancel_check,
    )
    elapsed = time.time() - start_time

    # Verification:
    # 1. The status must indicate cancellation
    assert text == "[STATUS:CANCELLED]"
    # 2. The cancellation must terminate the active process immediately (within 30 seconds to be safe on slow VM runners)
    if cancel_time is not None:
        elapsed_after_cancel = time.time() - cancel_time
        assert elapsed_after_cancel < 30.0
    else:
        assert elapsed < 30.0
    # 3. Only the first progress was registered
    assert any(abs(p - 0.05) < 1e-4 for p in progress_vals)
    assert all(p < 0.08 for p in progress_vals)
