import os
from unittest.mock import MagicMock, patch


import tempfile
from pathlib import Path
from app.core.db import Database
from app.core.cache import CacheManager
from app.core.history import HistoryManager

_test_dir = tempfile.mkdtemp()
db = Database(Path(_test_dir) / "test.db")
cache_manager = CacheManager(str(Path(_test_dir) / "cache.db"))
history_manager = HistoryManager(db, cache_manager, str(Path(_test_dir) / "history.db"))
def save_cache_sync(*args, **kwargs):
    cache_manager.save_cache_sync(*args, **kwargs)

from app.core.link_manager import LinkManager
from app.core.mover import execute_moves
from app.core.scanner import get_files_recursively

try:
    import pylnk3
except ImportError:
    pylnk3 = None


def test_relative_symlink_update(tmp_path):
    if __import__("sys").platform == "win32":
        __import__("pytest").skip("Windows requires admin privileges to create symlinks")
    # Setup
    base_dir = str(tmp_path)

    # Create target file
    target_dir = os.path.join(base_dir, "target_dir")
    os.makedirs(target_dir)
    target_file = os.path.join(target_dir, "data.txt")
    with open(target_file, "w") as f:
        f.write("content")

    # Create relative symlink in root
    symlink_path = os.path.join(base_dir, "link.txt")
    os.symlink("target_dir/data.txt", symlink_path)

    # Scan files to register links
    get_files_recursively(base_dir)
    assert LinkManager.get_link_info(symlink_path) is not None

    # We want to move link.txt into deeper_dir/link.txt
    plan = {
        "deeper_dir": {
            "link.txt": {
                "__type__": "file",
                "status": "Pending Move",
                "source_path": "link.txt",
                "target_filename": "link.txt",
            }
        },
        "target_dir": {"data.txt": None},
    }

    execute_moves(base_dir, plan, db, history_manager)

    # Verify link was moved and updated
    new_symlink_path = os.path.join(base_dir, "deeper_dir", "link.txt")
    assert os.path.exists(new_symlink_path)
    assert os.path.islink(new_symlink_path)

    # Check new target
    new_target = os.readlink(new_symlink_path)
    assert new_target == os.path.join("..", "target_dir", "data.txt")


def test_windows_shortcut_update_mocked(tmp_path):
    # Setup
    base_dir = str(tmp_path)

    # Create target file
    target_dir = os.path.join(base_dir, "target_dir")
    os.makedirs(target_dir)
    target_file = os.path.join(target_dir, "app.exe")
    with open(target_file, "w") as f:
        f.write("exe content")

    # Do NOT create dummy .lnk file on disk to satisfy constraint:
    # "Tests must not write physical .lnk files to the filesystem"
    shortcut_path = os.path.join(base_dir, "app.lnk")

    # Mock pylnk3
    mock_pylnk3 = MagicMock()
    mock_parsed = MagicMock()
    mock_parsed.path = os.path.join(target_dir, "app.exe")
    mock_parsed.arguments = "--headless"
    mock_parsed.description = "Test App"
    mock_parsed.icon = "app.ico"
    mock_parsed.icon_index = 2
    mock_parsed.work_dir = "C:\\"
    mock_parsed.window_mode = 3

    mock_pylnk3.parse.return_value = mock_parsed

    original_exists = os.path.exists
    original_remove = os.remove

    with patch("app.core.link_manager.pylnk3", mock_pylnk3), \
         patch("app.core.mover.pylnk3", mock_pylnk3), \
         patch("app.core.mover.os.path.exists") as mock_exists, \
         patch.object(history_manager, "create_snapshot"), \
         patch("app.core.mover.os.remove") as mock_remove, \
         patch("app.core.mover.shutil.move") as mock_shutil_move:
         
        # Make os.path.exists return True for our mocked shortcut and fallback to real os.path.exists for others
        def side_effect_exists(path):
            if path == shortcut_path:
                return True
            return original_exists(path)
        mock_exists.side_effect = side_effect_exists
        
        # Make os.remove do nothing for our mocked shortcut, fallback to real os.remove for others
        def side_effect_remove(path):
            if path == shortcut_path:
                return
            original_remove(path)
        mock_remove.side_effect = side_effect_remove

        # 1. Register link
        LinkManager.register_link(base_dir, "app.lnk")
        info = LinkManager.get_link_info(shortcut_path)
        assert info is not None
        assert info["type"] == "lnk"
        assert info["target"] == os.path.join(target_dir, "app.exe")

        # 2. Plan move for shortcut to a subfolder
        plan = {
            "subfolder": {
                "app.lnk": {
                    "__type__": "file",
                    "status": "Pending Move",
                    "source_path": "app.lnk",
                    "target_filename": "app.lnk",
                }
            },
            "target_dir": {"app.exe": None},
        }

        # 3. Execute move
        execute_moves(base_dir, plan, db, history_manager)
        
        print("MOCK CALLED WITH:", mock_pylnk3.for_file.call_args)
        print("SHUTIL MOVE CALLED WITH:", mock_shutil_move.call_args)
        
        # 4. Assertions
        dest_path = os.path.join(base_dir, "subfolder", "app.lnk")

        # Verify parsed was called on source
        mock_pylnk3.parse.assert_called_with(shortcut_path)

        # Verify for_file was called with original properties and new target
        # Note: we aren't moving the target in this test, so it should use the same target
        mock_pylnk3.for_file.assert_called_with(
            os.path.join(base_dir, "target_dir", "app.exe"),
            lnk_name=dest_path,
            arguments="--headless",
            description="Test App",
            icon_file="app.ico",
            icon_index=2,
            work_dir="C:\\",
            window_mode=3
        )

        # Mover expects to delete the original source file because moved_as_link = True
        # In actual code, for_file creates dest_path. Here it's mocked, so we just check source_path is deleted
        mock_remove.assert_any_call(shortcut_path)


def test_windows_shortcut_update_in_place_mocked(tmp_path):
    # Setup
    base_dir = str(tmp_path)

    # Create target file and move it
    target_dir = os.path.join(base_dir, "target_dir")
    new_target_dir = os.path.join(base_dir, "new_target_dir")
    os.makedirs(target_dir)
    os.makedirs(new_target_dir)
    
    target_file = os.path.join(target_dir, "app.exe")
    with open(target_file, "w") as f:
        f.write("exe content")

    shortcut_path = os.path.join(base_dir, "app.lnk")

    mock_pylnk3 = MagicMock()
    mock_parsed = MagicMock()
    mock_parsed.path = os.path.join(target_dir, "app.exe")
    mock_parsed.arguments = None
    mock_parsed.description = None
    mock_parsed.icon = None
    mock_parsed.icon_index = 0
    mock_parsed.work_dir = None
    mock_parsed.window_mode = None
    mock_pylnk3.parse.return_value = mock_parsed

    original_exists = os.path.exists
    original_remove = os.remove

    with patch("app.core.link_manager.pylnk3", mock_pylnk3), \
         patch("app.core.mover.pylnk3", mock_pylnk3), \
         patch("app.core.mover.os.path.exists") as mock_exists, \
         patch("app.core.mover.os.path.samefile") as mock_samefile, \
         patch.object(history_manager, "create_snapshot"), \
         patch("app.core.mover.shutil.move"), \
         patch("app.core.mover.os.remove") as mock_remove:
         
        def side_effect_exists(path):
            if path == shortcut_path:
                return True
            return original_exists(path)
        mock_exists.side_effect = side_effect_exists
        
        def side_effect_samefile(f1, f2):
            if f1 == shortcut_path and f2 == shortcut_path:
                return True
            if not original_exists(f1) or not original_exists(f2):
                return False
            return os.path.samefile(f1, f2)
        mock_samefile.side_effect = side_effect_samefile
        
        def side_effect_remove(path):
            if path == shortcut_path:
                return
            original_remove(path)
        mock_remove.side_effect = side_effect_remove

        LinkManager.register_link(base_dir, "app.lnk")

        # The shortcut stays in place, but its target moves
        plan = {
            "app.lnk": {
                "__type__": "file",
                "status": "Already Sorted",
                "source_path": "app.lnk",
                "target_filename": "app.lnk",
            },
            "new_target_dir": {
                "target_dir/app.exe": {
                    "__type__": "file",
                    "status": "Pending Move",
                    "source_path": "target_dir/app.exe",
                    "target_filename": "app.exe",
                }
            }
        }
        
        # When moving in place, the original file is deleted *before* for_file is called, 
        # so we need to be careful that our mock doesn't fail. The real `for_file` would recreate it.
        # However, the script checks if dest_path != source_path at the end to delete the original.
        
        execute_moves(base_dir, plan, db, history_manager)

        # verify that for_file was called with the new target path
        mock_pylnk3.for_file.assert_called_with(
            os.path.join(base_dir, "new_target_dir", "app.exe"),
            lnk_name=shortcut_path,
            arguments=None,
            description=None,
            icon_file=None,
            icon_index=0,
            work_dir=None,
            window_mode=None
        )


def test_windows_shortcut_update_exception(tmp_path, caplog):
    # Setup
    base_dir = str(tmp_path)
    target_dir = os.path.join(base_dir, "target_dir")
    os.makedirs(target_dir)
    target_file = os.path.join(target_dir, "app.exe")
    with open(target_file, "w") as f:
        f.write("content")

    shortcut_path = os.path.join(base_dir, "app.lnk")

    mock_pylnk3 = MagicMock()
    mock_parsed = MagicMock()
    mock_parsed.path = os.path.join(target_dir, "app.exe")
    mock_pylnk3.parse.return_value = mock_parsed

    # Simulate for_file throwing an exception
    mock_pylnk3.for_file.side_effect = Exception("Mocked Windows shortcut exception")

    original_exists = os.path.exists
    original_remove = os.remove

    with patch("app.core.link_manager.pylnk3", mock_pylnk3), \
         patch("app.core.mover.pylnk3", mock_pylnk3), \
         patch("app.core.mover.os.path.exists") as mock_exists, \
         patch("app.core.mover.os.path.samefile") as mock_samefile, \
         patch.object(history_manager, "create_snapshot"), \
         patch("app.core.mover.shutil.move"), \
         patch("app.core.mover.os.remove") as mock_remove:
         
        def side_effect_exists(path):
            if path == shortcut_path:
                return True
            return original_exists(path)
        mock_exists.side_effect = side_effect_exists
        
        def side_effect_samefile(f1, f2):
            if f1 == shortcut_path and f2 == shortcut_path:
                return True
            if not original_exists(f1) or not original_exists(f2):
                return False
            return os.path.samefile(f1, f2)
        mock_samefile.side_effect = side_effect_samefile
        
        def side_effect_remove(path):
            if path == shortcut_path:
                return
            original_remove(path)
        mock_remove.side_effect = side_effect_remove

        LinkManager.register_link(base_dir, "app.lnk")

        plan = {
            "subfolder": {
                "app.lnk": {
                    "__type__": "file",
                    "status": "Pending Move",
                    "source_path": "app.lnk",
                    "target_filename": "app.lnk",
                }
            }
        }
        
        execute_moves(base_dir, plan, db, history_manager)
        
        assert "Failed to update Windows shortcut" in caplog.text
        assert "Mocked Windows shortcut exception" in caplog.text
