import os
import sys
from unittest.mock import MagicMock, patch, mock_open
import pytest

from app.core.path_utils import is_non_local_path
from app.core.history import HistoryManager, DummyHistoryManager
from app.core.session import AppSession
from app.config import AppSettings


def test_is_non_local_path_unc():
    # UNC paths should always be flagged as non-local
    assert is_non_local_path(r"\\server\share") is True
    assert is_non_local_path("//server/share") is True
    # Empty paths or None are not non-local
    assert is_non_local_path("") is False
    assert is_non_local_path(None) is False


def test_is_non_local_path_windows_drive_types():
    with patch("sys.platform", "win32"), \
         patch("os.path.splitdrive", return_value=("D:", "test")), \
         patch("ctypes.windll", create=True) as mock_windll:
        
        # Test DRIVE_REMOVABLE = 2
        mock_windll.kernel32.GetDriveTypeW.return_value = 2
        assert is_non_local_path(r"D:\some\path") is True
        mock_windll.kernel32.GetDriveTypeW.assert_called_with("D:\\")
        
        # Test DRIVE_REMOTE = 4
        mock_windll.kernel32.GetDriveTypeW.return_value = 4
        assert is_non_local_path(r"D:\some\path") is True
        
        # Test DRIVE_CDROM = 5
        mock_windll.kernel32.GetDriveTypeW.return_value = 5
        assert is_non_local_path(r"D:\some\path") is True

        # Test DRIVE_FIXED = 3 (local)
        mock_windll.kernel32.GetDriveTypeW.return_value = 3
        assert is_non_local_path(r"D:\some\path") is False


def test_is_non_local_path_unix_prefixes():
    with patch("sys.platform", "linux"):
        # Volumes, media, and mnt prefixes
        assert is_non_local_path("/Volumes/USB/Folder") is True
        assert is_non_local_path("/media/external/Folder") is True
        assert is_non_local_path("/mnt/nas/Folder") is True
        # Standard local home/tmp directory should not be non-local
        assert is_non_local_path("/home/user/Folder") is False


def test_is_non_local_path_linux_proc_mounts():
    proc_mounts_content = """
/dev/sda1 / ext4 rw,relatime 0 0
//192.168.1.100/share /mnt/cifs cifs rw,relatime 0 0
/dev/sdb1 /home/user/usb vfat rw,relatime 0 0
"""
    with patch("sys.platform", "linux"), \
         patch("os.path.exists", side_effect=lambda p: p in ("/proc/mounts", "/home/user/usb")), \
         patch("os.path.ismount", side_effect=lambda p: p in ("/", "/mnt/cifs", "/home/user/usb")), \
         patch("builtins.open", mock_open(read_data=proc_mounts_content)):
        
        # Test a path mounted as cifs
        with patch("os.path.abspath", return_value="/mnt/cifs/documents"):
            assert is_non_local_path("/mnt/cifs/documents") is True
            
        # Test a path mounted as vfat
        with patch("os.path.abspath", return_value="/home/user/usb/files"):
            assert is_non_local_path("/home/user/usb/files") is True

        # Test standard local path (ext4)
        with patch("os.path.abspath", return_value="/"):
            assert is_non_local_path("/some/local/path") is False


def test_dummy_history_manager_behavior():
    # Verify properties and no-op behavior of DummyHistoryManager
    dummy_manager = DummyHistoryManager(db=MagicMock(), cache_manager=MagicMock(), db_path="/tmp/history.db")
    
    assert dummy_manager.is_bypass is True
    assert HistoryManager.is_bypass is False
    
    # Assert create_snapshot returns bypass session
    assert dummy_manager.create_snapshot("/some/dir") == "bypass_session"
    
    # Assert rollback is safe no-op
    try:
        dummy_manager.rollback("bypass_session")
    except Exception as e:
        pytest.fail(f"rollback raised unexpected exception: {e}")
        
    # Assert other methods are safe and return empty/default values
    assert dummy_manager.check_missing_files("bypass_session") == []
    assert dummy_manager.get_sessions() == []


def test_session_history_bypass_selection(tmp_path):
    settings = AppSettings()
    
    # Test local path (instantiates regular HistoryManager)
    local_path = str(tmp_path / "local_dir")
    os.makedirs(local_path, exist_ok=True)
    
    session_local = AppSession(settings, base_dir=local_path)
    assert session_local.history_manager.is_bypass is False
    assert isinstance(session_local.history_manager, HistoryManager)
    session_local.close()
    
    # Test non-local path (instantiates DummyHistoryManager)
    with patch("app.core.path_utils.is_non_local_path", return_value=True):
        session_remote = AppSession(settings, base_dir=local_path)
        assert session_remote.history_manager.is_bypass is True
        assert isinstance(session_remote.history_manager, DummyHistoryManager)
        session_remote.close()
