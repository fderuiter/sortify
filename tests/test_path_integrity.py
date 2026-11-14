import os
import sys
import tempfile
import time

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
                "relative_source": "../folder2/fileB.txt",
                "target_filename": "fileA.txt",
            }
        },
        "folder2": {
            "folder1/fileA.txt": {
                "__type__": "file",
                "relative_source": "../folder1/fileA.txt",
                "target_filename": "fileB.txt",
            }
        },
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
        path=os.path.abspath("/base/dir/folder1"), is_dir=False, inode=1234, size=100
    )

    plan = {
        "folder1": {
            "subfolder": {
                "source_file.txt": {
                    "__type__": "file",
                    "relative_source": "source_file.txt",
                    "target_filename": "file.txt",
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


def test_collision_check_linear_scaling_2000_moves():
    """Verify check_collisions scales linearly with 2000 simulated moves and preserves alerts."""
    base_dir = "/base/dir"
    tracker = VirtualFilesystemTracker()

    moves_list = []
    for i in range(1999):
        moves_list.append(
            (
                f"file_{i}.txt",
                f"/base/dir/file_{i}.txt",
                f"/base/dir/file_{i + 1}.txt",
            )
        )
    blocked_path = "/base/dir/blocked_unmoved.txt"
    moves_list.append(
        (
            "file_1999.txt",
            "/base/dir/file_1999.txt",
            blocked_path,
        )
    )

    tracker.populate_from_moves(moves_list)

    # Add the unmoving target file into tracker nodes so it is an existing file
    tracker.nodes[os.path.abspath(blocked_path)] = VirtualNode(
        path=os.path.abspath(blocked_path),
        is_dir=False,
        inode=99999,
        size=1024,
    )

    # Suspend any active profiler/coverage tracer during microsecond/millisecond benchmarking
    orig_trace = sys.gettrace()
    try:
        sys.settrace(None)
        start_time = time.perf_counter()
        collisions = tracker.check_collisions(moves_list, base_dir)
        elapsed_time = time.perf_counter() - start_time
    finally:
        sys.settrace(orig_trace)

    # Collisions should preserve alert for unmoving destination file
    assert len(collisions) == 1
    assert collisions[0]["type"] == "target_collision"
    assert collisions[0]["path"] == os.path.abspath(blocked_path)
    assert collisions[0]["source"] == os.path.abspath("/base/dir/file_1999.txt")
    assert (
        "blocked by an existing file that is not being moved"
        in collisions[0]["message"]
    )

    # Linear scaling requirement: 2000 moves should execute in well under 0.25s (typically < 0.05s)
    assert elapsed_time < 0.25, (
        f"check_collisions took too long: {elapsed_time:.4f}s for 2000 moves (expected < 0.25s)"
    )


def test_broken_symlink_detection():
    base_dir = "/base/dir"

    # Register a relative symlink from sym_link.txt pointing to target.txt
    LinkManager.clear()
    LinkManager._registry[os.path.abspath("/base/dir/sym_link.txt")] = {
        "type": "symlink",
        "target": "target.txt",
    }

    # Move sym_link.txt to deeper/sym_link.txt, but target.txt is not in the plan nor does it exist.
    plan = {
        "deeper": {
            "sym_link.txt": {
                "__type__": "file",
                "relative_source": "../sym_link.txt",
                "target_filename": "sym_link.txt",
            }
        }
    }

    result = VerificationEngine.verify_plan_integrity(base_dir, plan)

    assert result["success"] is False
    assert len(result["broken_links"]) > 0
    assert any(
        link_item["type"] == "broken_symlink" for link_item in result["broken_links"]
    )
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
                "target_filename": "real.txt",
            }
        }

        # Capture directory snapshot
        initial_files = set(os.listdir(tmp_dir))

        # Run simulation
        _ = VerificationEngine.verify_plan_integrity(tmp_dir, plan)

        # Physical filesystem must remain completely unaltered!
        final_files = set(os.listdir(tmp_dir))
        assert initial_files == final_files
        assert os.path.exists(real_file)
        if __import__("sys").platform != "win32":
            assert os.path.islink(real_link)


def test_safe_relpath(monkeypatch):
    from app.core.path_utils import safe_relpath

    # Standard relative path
    rel = safe_relpath("/app/tests/file.txt", "/app")
    assert rel in ("tests/file.txt", "tests\\file.txt")

    # Empty path or start=None
    assert safe_relpath("") == ""
    assert safe_relpath("/app/file.txt", None) == os.path.abspath("/app/file.txt")

    # Cross-drive ValueError simulation on Windows
    def mock_relpath(path, start):
        raise ValueError("path is on mount 'D:', start is on mount 'C:'")

    monkeypatch.setattr(os.path, "relpath", mock_relpath)
    fallback = safe_relpath("D:\\data\\file.txt", "C:\\Users")
    assert fallback == os.path.abspath("D:\\data\\file.txt")
