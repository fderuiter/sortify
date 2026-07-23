import os
import tempfile

from app.core.link_manager import LinkManager
from app.core.verifier import VerificationEngine, VirtualFilesystemTracker, VirtualNode


def test_circular_rename_dependency():
    base_dir = "/base/dir"
    
    # We construct a plan that forms a cycle:
    # folder2/fileB.txt moves to folder1/fileA.txt,
    # and folder1/fileA.txt moves to folder2/fileB.txt.
    plan = {
        "folder1": {
            "folder2/fileB.txt": {
                "__type__": "file",
                "target_filename": "fileA.txt"
            }
        },
        "folder2": {
            "folder1/fileA.txt": {
                "__type__": "file",
                "target_filename": "fileB.txt"
            }
        }
    }
    
    result = VerificationEngine.verify_plan_integrity(base_dir, plan)
    
    assert result["success"] is False
    assert len(result["circular_renames"]) > 0
    assert any("Circular renaming dependency" in w for w in result["warnings"])


def test_parent_directory_collision():
    base_dir = "/base/dir"
    
    # We simulate a case where "folder1" is actually a file, not a directory.
    # Therefore, we cannot move another file to "folder1/subfolder/file.txt".
    tracker = VirtualFilesystemTracker()
    tracker.nodes[os.path.abspath("/base/dir/folder1")] = VirtualNode(
        path=os.path.abspath("/base/dir/folder1"),
        is_dir=False,
        inode=1234,
        size=100
    )
    
    plan = {
        "folder1": {
            "subfolder": {
                "source_file.txt": {
                    "__type__": "file",
                    "target_filename": "file.txt"
                }
            }
        }
    }
    
    moves_list = VerificationEngine.get_moves(base_dir, plan)
    tracker.populate_from_moves(moves_list)
    collisions = tracker.check_collisions(moves_list, base_dir)
    
    assert len(collisions) > 0
    assert any(c["type"] == "parent_directory_collision" for c in collisions)
    assert "blocked because ancestor" in collisions[0]["message"]


def test_broken_symlink_detection():
    base_dir = "/base/dir"
    
    # Register a relative symlink from sym_link.txt pointing to target.txt
    LinkManager.clear()
    LinkManager._registry[os.path.abspath("/base/dir/sym_link.txt")] = {
        "type": "symlink",
        "target": "target.txt"
    }
    
    # Move sym_link.txt to deeper/sym_link.txt, but target.txt is not in the plan nor does it exist.
    plan = {
        "deeper": {
            "sym_link.txt": {
                "__type__": "file",
                "target_filename": "sym_link.txt"
            }
        }
    }
    
    result = VerificationEngine.verify_plan_integrity(base_dir, plan)
    
    assert result["success"] is False
    assert len(result["broken_links"]) > 0
    assert any(l["type"] == "broken_symlink" for l in result["broken_links"])
    assert "Broken symlink target" in result["warnings"][0]


def test_physical_filesystem_remains_unaltered():
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Create some real files in the directory
        real_file = os.path.join(tmp_dir, "real.txt")
        with open(real_file, "w") as f:
            f.write("content")
            
        real_link = os.path.join(tmp_dir, "link.txt")
        if __import__("sys").platform != "win32":
            os.symlink("real.txt", real_link)
        else:
            with open(real_link, "w") as f:
                f.write("mock link")
                
        # Register them
        LinkManager.clear()
        if __import__("sys").platform != "win32":
            LinkManager.register_link(tmp_dir, "link.txt")
            
        # Design a plan with collisions and circular renames
        plan = {
            "nonexistent_folder/sub/real.txt": {
                "__type__": "file",
                "source_path": "real.txt",
                "target_filename": "real.txt"
            }
        }
        
        # Capture directory snapshot
        initial_files = set(os.listdir(tmp_dir))
        
        # Run simulation
        result = VerificationEngine.verify_plan_integrity(tmp_dir, plan)
        
        # Physical filesystem must remain completely unaltered!
        final_files = set(os.listdir(tmp_dir))
        assert initial_files == final_files
        assert os.path.exists(real_file)
        if __import__("sys").platform != "win32":
            assert os.path.islink(real_link)
