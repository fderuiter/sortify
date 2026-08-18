import os

from app.core.verifier import VerificationEngine


def test_get_moves_flattens_plan():
    base_dir = os.path.normpath("/base/dir")
    plan = {
        "file1.txt": None,
        "folder1": {
            "file2.txt": {
                "__type__": "file",
                "relative_source": "file2.txt",
                "target_filename": "renamed2.txt",
            },
            "subfolder": {
                "file3.txt": {
                    "__type__": "file",
                    "relative_source": "file3.txt",
                }
            },
        },
        "folder2": {"__type__": "directory"},
    }

    engine = VerificationEngine()
    moves = engine.get_moves(base_dir, plan)

    # Sort moves for deterministic assertion
    moves.sort(key=lambda x: x[0])

    assert len(moves) == 3

    # Check file1.txt
    assert moves[0] == (
        "file1.txt",
        os.path.join(base_dir, "file1.txt"),
        os.path.join(base_dir, "file1.txt"),
    )

    # Check file2.txt
    assert moves[1] == (
        "file2.txt",
        os.path.join(base_dir, "folder1", "file2.txt"),
        os.path.join(base_dir, "folder1", "renamed2.txt"),
    )

    # Check file3.txt
    assert moves[2] == (
        "file3.txt",
        os.path.join(base_dir, "folder1", "subfolder", "file3.txt"),
        os.path.join(base_dir, "folder1", "subfolder", "file3.txt"),
    )


def test_path_length_validation_and_simulation():
    from app.core.path_utils import is_path_too_long
    from app.core.verifier import VerificationEngine

    # Test is_path_too_long helper platform-independently
    root_abs = os.path.abspath("/")
    chars_for_limit = 260 - len(root_abs)
    assert is_path_too_long(root_abs + "a" * chars_for_limit) is True
    assert is_path_too_long(root_abs + "a" * (chars_for_limit - 1)) is False
    assert is_path_too_long("") is False

    # Test VirtualFilesystemTracker / VerificationEngine path length check simulation
    base_dir = "/base/dir"
    # Construct a destination path that exceeds 260 characters
    long_filename = "x" * 250 + ".txt"
    plan = {
        "short_file.txt": {
            "__type__": "file",
            "relative_source": "short_file.txt",
            "target_filename": long_filename,
        }
    }

    result = VerificationEngine.verify_plan_integrity(base_dir, plan)
    assert result["success"] is False
    assert len(result["long_paths"]) > 0
    assert result["long_paths"][0]["path"].endswith(long_filename)
    assert any(
        "exceeds the standard Windows character limit" in w for w in result["warnings"]
    )


def test_nested_directory_dict_traversal():
    """Verify that get_moves recursively inspects directory objects with __type__ == 'directory'."""
    base_dir = os.path.normpath("/base/dir")
    plan = {
        "folder1": {
            "__type__": "directory",
            "source_path": os.path.join(base_dir, "folder1"),
            "status": "To Be Deleted",
            "protected": False,
            "fileA.txt": {
                "__type__": "file",
                "relative_source": "fileA.txt",
                "target_filename": "renamedA.txt",
            },
            "subfolder": {
                "__type__": "directory",
                "fileB.txt": {
                    "__type__": "file",
                    "relative_source": "fileB.txt",
                },
            },
        },
    }

    moves = VerificationEngine.get_moves(base_dir, plan)
    moves.sort(key=lambda x: x[0])

    assert len(moves) == 2

    assert moves[0] == (
        "fileA.txt",
        os.path.normpath(os.path.join(base_dir, "folder1", "fileA.txt")),
        os.path.normpath(os.path.join(base_dir, "folder1", "renamedA.txt")),
    )

    assert moves[1] == (
        "fileB.txt",
        os.path.normpath(os.path.join(base_dir, "folder1", "subfolder", "fileB.txt")),
        os.path.normpath(os.path.join(base_dir, "folder1", "subfolder", "fileB.txt")),
    )


def test_nested_subdirectory_collision_detection():
    """Verify that duplicate target paths in nested subdirectories are flagged as collisions."""
    base_dir = os.path.normpath("/base/dir")
    plan = {
        "folderA": {
            "__type__": "directory",
            "file1.txt": {
                "__type__": "file",
                "relative_source": "file1.txt",
                "target_filename": "collision.txt",
            },
        },
        "folderB": {
            "__type__": "directory",
            "file2.txt": {
                "__type__": "file",
                "relative_source": "file2.txt",
                "target_filename": "../folderA/collision.txt",
            },
        },
    }

    result = VerificationEngine.verify_plan_integrity(base_dir, plan)
    assert result["success"] is False
    assert len(result["collisions"]) == 1
    assert result["collisions"][0]["type"] == "duplicate_target"
    assert any("Multiple files are planned to be moved" in w for w in result["warnings"])


def test_nested_item_missing_relative_source_fails_validation():
    """Verify that nested items inside folder dictionary objects missing relative_source trigger failure."""
    base_dir = os.path.normpath("/base/dir")
    plan = {
        "folder": {
            "__type__": "directory",
            "invalid_file.txt": {
                "__type__": "file",
                "target_filename": "moved.txt",
            },
        }
    }

    result = VerificationEngine.verify_plan_integrity(base_dir, plan)
    assert result["success"] is False
    assert any(
        "Missing required relative source metadata field for nested item 'invalid_file.txt'" in w
        for w in result["warnings"]
    )


def test_plan_verification_in_memory_without_disk_scan():
    """Verify plan verification operates purely in-memory without disk directory scans."""
    non_existent_dir = os.path.normpath("/non_existent_path_xyz_12345")
    plan = {
        "folder1": {
            "__type__": "directory",
            "file1.txt": {
                "__type__": "file",
                "relative_source": "file1.txt",
                "target_filename": "output.txt",
            },
            "file2.txt": {
                "__type__": "file",
                "relative_source": "file2.txt",
                "target_filename": "output.txt",
            },
        }
    }

    result = VerificationEngine.verify_plan_integrity(non_existent_dir, plan)
    assert result["success"] is False
    assert len(result["collisions"]) == 1
