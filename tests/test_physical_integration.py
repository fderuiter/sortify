import os
from pathlib import Path
import pytest
from app.core.mover import execute_moves
from app.core.verifier import VerificationEngine

def test_physical_move(tmp_path):
    """Test that files are physically relocated from local source to local destination."""
    source_file = tmp_path / "source.txt"
    source_file.write_text("hello")
    
    plan = {
        "target_folder": {
            "source.txt": {
                "__type__": "file",
                "target_filename": "source.txt"
            }
        }
    }
    
    execute_moves(str(tmp_path), plan)
    
    assert not os.path.exists(source_file)
    assert os.path.exists(tmp_path / "target_folder" / "source.txt")
    assert (tmp_path / "target_folder" / "source.txt").read_text() == "hello"

def test_naming_collision_resolution(tmp_path):
    """Test that naming collisions create files with numeric suffixes."""
    # Create the source file
    source_file = tmp_path / "source.txt"
    source_file.write_text("new content")
    
    # Create an existing file in the target folder with the same name
    target_folder = tmp_path / "target_folder"
    target_folder.mkdir()
    target_file = target_folder / "source.txt"
    target_file.write_text("existing content")
    
    plan = {
        "target_folder": {
            "source.txt": {
                "__type__": "file",
                "target_filename": "source.txt"
            }
        }
    }
    
    execute_moves(str(tmp_path), plan)
    
    # Both files should exist in the target folder
    assert target_file.read_text() == "existing content"
    
    # The new file should have a suffix
    suffixed_file = target_folder / "source_1.txt"
    assert suffixed_file.exists()
    assert suffixed_file.read_text() == "new content"
    
    # The source file should be gone
    assert not source_file.exists()

def test_empty_source_folder_cleanup(tmp_path):
    """Test that empty source folders are recursively deleted after moving."""
    nested_dir = tmp_path / "a" / "b" / "c"
    nested_dir.mkdir(parents=True)
    
    source_file = nested_dir / "move_me.txt"
    source_file.write_text("content")
    
    # Ensure source directories exist before move
    assert nested_dir.exists()
    
    plan = {
        "target_folder": {
            "a/b/c/move_me.txt": {
                "__type__": "file",
                "target_filename": "move_me.txt"
            }
        },
        "a_dir": {
            "__type__": "directory",
            "source_path": str(tmp_path / "a"),
            "status": "To Be Deleted"
        },
        "b_dir": {
            "__type__": "directory",
            "source_path": str(tmp_path / "a" / "b"),
            "status": "To Be Deleted"
        },
        "c_dir": {
            "__type__": "directory",
            "source_path": str(tmp_path / "a" / "b" / "c"),
            "status": "To Be Deleted"
        }
    }
    
    summary = execute_moves(str(tmp_path), plan)
    
    # The target file should exist
    assert (tmp_path / "target_folder" / "move_me.txt").exists()
    
    # The source directories should be deleted
    assert not (tmp_path / "a" / "b" / "c").exists()
    assert not (tmp_path / "a" / "b").exists()
    assert not (tmp_path / "a").exists()
    
    # Expect 3 deleted folders
    assert summary["deleted_folders"] == 3

def test_cloud_folder_detection(tmp_path, monkeypatch):
    """Test that targets in cloud sync paths are identified."""
    verifier = VerificationEngine()
    
    # Test macOS iCloud path
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    icloud_path = str(tmp_path / "Library" / "Mobile Documents" / "target")
    assert verifier.is_cloud_path(icloud_path) is True
    
    # Test Windows OneDrive path using the fallback logic, but manually setting the path separator to match Windows style
    monkeypatch.setattr("platform.system", lambda: "Windows")
    # For testing the fallback `\\onedrive\\` logic when running on linux, we can just inject a string that resembles a Windows path.
    # However, since `os.path.normpath(os.path.abspath(path))` is called in the verifier, we will just use the env var method for Windows.
    monkeypatch.setenv("OneDrive", str(tmp_path / "MyCloudFolder"))
    env_onedrive_path = str(tmp_path / "MyCloudFolder" / "target")
    assert verifier.is_cloud_path(env_onedrive_path) is True
    
    # Test safe path
    safe_path = str(tmp_path / "Documents" / "target")
    assert verifier.is_cloud_path(safe_path) is False
