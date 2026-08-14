import sys
import os
import stat
import gc
import time
import pytest
from unittest.mock import MagicMock, patch

from app.core.resilient_file_ops import (
    resilient_move,
    resilient_remove,
    resilient_rmtree,
    resilient_file_hash,
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
