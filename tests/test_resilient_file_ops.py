import os
import stat
from unittest.mock import patch

import pytest

from app.core.resilient_file_ops import (
    resilient_file_hash,
    resilient_move,
    resilient_remove,
    resilient_rmtree,
)


def test_resilient_remove_success(tmp_path):
    # Test successful removal of a file
    f = tmp_path / "test_file.txt"
    f.write_text("hello")
    assert f.exists()
    resilient_remove(str(f))
    assert not f.exists()


def test_resilient_remove_dir_success(tmp_path):
    # Test successful removal of an empty directory
    d = tmp_path / "test_dir"
    d.mkdir()
    assert d.exists()
    resilient_remove(str(d))
    assert not d.exists()


@patch("app.core.resilient_file_ops.IS_WINDOWS", True)
@patch("app.core.resilient_file_ops.MAX_ATTEMPTS", 5)
@patch("app.core.resilient_file_ops.RETRY_DELAY", 0.01)
@patch("gc.collect")
@patch("time.sleep")
def test_resilient_remove_permission_error_chmod(mock_sleep, mock_collect, tmp_path):
    # Test that read-only permission issues are corrected dynamically
    f = tmp_path / "readonly_file.txt"
    f.write_text("cannot delete me easily")

    # Make it read-only
    f.chmod(stat.S_IREAD)

    # We mock os.remove to raise PermissionError first, then succeed after chmod
    original_remove = os.remove
    calls = []

    def mock_remove(path):
        calls.append(path)
        # Raise PermissionError on the first call
        if len(calls) == 1:
            raise PermissionError("Permission denied")
        return original_remove(path)

    with patch("os.remove", side_effect=mock_remove):
        resilient_remove(str(f))

    assert not f.exists()
    assert len(calls) == 2


@patch("app.core.resilient_file_ops.IS_WINDOWS", True)
@patch("app.core.resilient_file_ops.MAX_ATTEMPTS", 3)
@patch("app.core.resilient_file_ops.RETRY_DELAY", 0.01)
@patch("gc.collect")
@patch("time.sleep")
def test_resilient_remove_retry_gc_trigger(mock_sleep, mock_collect):
    # Test that garbage collection is triggered immediately before each retry
    # We raise OSError on all attempts
    with patch("os.remove", side_effect=OSError("Locked file")):
        with pytest.raises(OSError):
            resilient_remove("mock_locked_file.txt")

    # Max attempts = 3. First attempt fails. 2 retries are attempted.
    # Before each of the 2 retries, gc.collect() should be called.
    assert mock_collect.call_count == 2
    assert mock_sleep.call_count == 2


@patch("app.core.resilient_file_ops.IS_WINDOWS", False)
@patch("app.core.resilient_file_ops.MAX_ATTEMPTS", 1)
@patch("app.core.resilient_file_ops.RETRY_DELAY", 0.0)
@patch("gc.collect")
@patch("time.sleep")
def test_resilient_remove_non_windows_bypass_retry(mock_sleep, mock_collect):
    # On non-Windows platforms, retries and sleep are bypassed (MAX_ATTEMPTS = 1)
    with patch("os.remove", side_effect=OSError("Locked file")):
        with pytest.raises(OSError):
            resilient_remove("mock_locked_file.txt")

    assert mock_collect.call_count == 0
    assert mock_sleep.call_count == 0


@patch("app.core.resilient_file_ops.IS_WINDOWS", True)
@patch("app.core.resilient_file_ops.MAX_ATTEMPTS", 3)
@patch("app.core.resilient_file_ops.RETRY_DELAY", 0.01)
@patch("gc.collect")
@patch("time.sleep")
def test_resilient_move_success(mock_sleep, mock_collect, tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("move me")

    resilient_move(str(src), str(dst))
    assert not src.exists()
    assert dst.exists()
    assert dst.read_text() == "move me"


@patch("app.core.resilient_file_ops.IS_WINDOWS", True)
@patch("app.core.resilient_file_ops.MAX_ATTEMPTS", 3)
@patch("app.core.resilient_file_ops.RETRY_DELAY", 0.01)
@patch("gc.collect")
@patch("time.sleep")
def test_resilient_move_retry_gc_trigger(mock_sleep, mock_collect):
    # Test that move failure triggers gc.collect and sleeps
    # We patch shutil.move with a regular python function, so it's not a Mock instance
    def fake_move(s, d):
        raise OSError("Locked")

    with patch("os.replace", side_effect=OSError("Locked")):
        with patch("shutil.move", new=fake_move):
            with pytest.raises(OSError):
                resilient_move("src.txt", "dst.txt")

    assert mock_collect.call_count == 2
    assert mock_sleep.call_count == 2


def test_resilient_file_hash_success(tmp_path):
    f = tmp_path / "sample.txt"
    f.write_text("test content")
    h = resilient_file_hash(str(f))
    # SHA-256 hash of "test content"
    import hashlib

    expected = hashlib.sha256(b"test content").hexdigest()
    assert h == expected


def test_resilient_file_hash_normalize_text(tmp_path):
    f = tmp_path / "script.py"
    # Write file with CRLF line endings
    f.write_bytes(b"line1\r\nline2\r\n")

    import hashlib

    # With normalize_text=False, hash includes \r\n
    raw_hash = resilient_file_hash(str(f), normalize_text=False)
    expected_raw = hashlib.sha256(b"line1\r\nline2\r\n").hexdigest()
    assert raw_hash == expected_raw

    # With normalize_text=True, hash normalizes \r\n to \n
    norm_hash = resilient_file_hash(str(f), normalize_text=True)
    expected_norm = hashlib.sha256(b"line1\nline2\n").hexdigest()
    assert norm_hash == expected_norm


def test_resilient_file_hash_skip_media_tags(tmp_path):
    # Construct a mock MP3 with ID3 header
    id3_header = b"ID3\x03\x00\x00\x00\x00\x00\x0a" + b"1234567890"  # 10 byte header + 10 byte tag
    audio_data = b"AUDIO_DATA_PAYLOAD_12345"
    mp3_file = tmp_path / "test.mp3"
    mp3_file.write_bytes(id3_header + audio_data)

    import hashlib

    # With skip_media_tags=False (default), hash computes full file hash
    full_hash = resilient_file_hash(str(mp3_file), skip_media_tags=False)
    assert full_hash == hashlib.sha256(id3_header + audio_data).hexdigest()

    # With skip_media_tags=True, hash computes audio payload only
    payload_hash = resilient_file_hash(str(mp3_file), skip_media_tags=True)
    assert payload_hash == hashlib.sha256(audio_data).hexdigest()
    assert full_hash != payload_hash


@patch("app.core.resilient_file_ops.IS_WINDOWS", True)
@patch("app.core.resilient_file_ops.MAX_ATTEMPTS", 5)
@patch("app.core.resilient_file_ops.RETRY_DELAY", 0.01)
@patch("gc.collect")
@patch("time.sleep")
def test_resilient_file_hash_transient_permission_error_recovery(
    mock_sleep, mock_collect, tmp_path
):
    f = tmp_path / "locked.bin"
    f.write_bytes(b"locked_content_payload")

    import builtins
    import hashlib

    expected_hash = hashlib.sha256(b"locked_content_payload").hexdigest()

    real_open = builtins.open
    calls = []

    def mock_open_func(file_path, *args, **kwargs):
        calls.append(file_path)
        if len(calls) < 3:
            raise PermissionError("File in use by another process")
        return real_open(file_path, *args, **kwargs)

    import builtins

    with patch("builtins.open", side_effect=mock_open_func):
        computed_hash = resilient_file_hash(str(f))

    assert computed_hash == expected_hash
    assert len(calls) == 3
    assert mock_collect.call_count == 2
    assert mock_sleep.call_count == 2


def test_scanner_and_extractor_hash_alignment(tmp_path):
    from app.core.extractor import get_file_hash
    from app.core.forensic_scanner import ForensicScanner

    # Standard file: ForensicScanner and extractor.get_file_hash return identical full-file hashes
    txt_file = tmp_path / "document.txt"
    txt_file.write_text("Hello World Forensic")

    scanner_hash = ForensicScanner.compute_sha256(str(txt_file))
    extractor_hash = get_file_hash(str(txt_file))

    assert scanner_hash == extractor_hash

    # Media file: ForensicScanner computes full-file hash, extractor.get_file_hash skips tags
    id3_header = b"ID3\x03\x00\x00\x00\x00\x00\x0a" + b"1234567890"
    audio_data = b"AUDIO_DATA"
    mp3_file = tmp_path / "song.mp3"
    mp3_file.write_bytes(id3_header + audio_data)

    scanner_mp3_hash = ForensicScanner.compute_sha256(str(mp3_file))
    extractor_mp3_hash = get_file_hash(str(mp3_file))

    import hashlib

    assert scanner_mp3_hash == hashlib.sha256(id3_header + audio_data).hexdigest()
    assert extractor_mp3_hash == hashlib.sha256(audio_data).hexdigest()


@patch("app.core.resilient_file_ops.IS_WINDOWS", True)
@patch("app.core.resilient_file_ops.MAX_ATTEMPTS", 3)
@patch("app.core.resilient_file_ops.RETRY_DELAY", 0.01)
@patch("gc.collect")
@patch("time.sleep")
def test_resilient_file_hash_retry_and_gc(mock_sleep, mock_collect):
    # Test hashing fails initially and retries
    calls = []

    def mock_open(*args, **kwargs):
        calls.append(args)
        raise OSError("Sharing violation")

    with patch("builtins.open", side_effect=mock_open):
        resilient_file_hash("test.txt")

    # We should have seen MAX_ATTEMPTS calls (3)
    assert len(calls) == 3
    # GC should be called before each of the 2 retry attempts
    assert mock_collect.call_count == 2
    assert mock_sleep.call_count == 2


@patch("app.core.resilient_file_ops.IS_WINDOWS", False)
@patch("app.core.resilient_file_ops.MAX_ATTEMPTS", 1)
@patch("app.core.resilient_file_ops.RETRY_DELAY", 0.0)
@patch("gc.collect")
@patch("time.sleep")
def test_resilient_file_hash_non_windows_no_retry(mock_sleep, mock_collect):
    calls = []

    def mock_open(*args, **kwargs):
        calls.append(args)
        raise OSError("Sharing violation")

    with patch("builtins.open", side_effect=mock_open):
        resilient_file_hash("test.txt")

    # On non-Windows, it should bypass retries entirely and try exactly once
    assert len(calls) == 1
    assert mock_collect.call_count == 0
    assert mock_sleep.call_count == 0


def test_resilient_rmtree_readonly_contents(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    f = sub / "readonly.txt"
    f.write_text("read only content")
    f.chmod(stat.S_IREAD)

    assert f.exists()
    resilient_rmtree(str(tmp_path / "sub"))
    assert not sub.exists()


def test_resilient_rmtree_warning_logged_after_max_attempts(tmp_path, caplog):
    import logging

    d = tmp_path / "dummy_dir"
    d.mkdir()

    with caplog.at_level(logging.WARNING):
        with patch("shutil.rmtree", side_effect=OSError("Access denied")):
            with pytest.raises(OSError, match="Access denied"):
                resilient_rmtree(str(d))

    assert (
        f"Failed to rmtree {d} after" in caplog.text
        or f"Failed to rmtree <USER_HOME>/{d.name} after" in caplog.text
        or f"Failed to rmtree <USER_HOME>\\{d.name} after" in caplog.text
    )


def test_resilient_rmtree_ignore_errors(tmp_path, caplog):
    import logging

    d = tmp_path / "dummy_dir"
    d.mkdir()

    with caplog.at_level(logging.WARNING):
        with patch("shutil.rmtree", side_effect=OSError("Access denied")):
            resilient_rmtree(str(d), ignore_errors=True)

    assert (
        f"Failed to rmtree {d} after" in caplog.text
        or f"Failed to rmtree <USER_HOME>/{d.name} after" in caplog.text
        or f"Failed to rmtree <USER_HOME>\\{d.name} after" in caplog.text
    )
