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

    # Test is_path_too_long helper using a custom limit to ensure platform-independent length evaluation
    base_abs = os.path.abspath("/")
    too_long_path = base_abs + "a" * 10
    safe_path = base_abs + "a" * 9
    custom_limit = len(base_abs) + 10
    assert is_path_too_long(too_long_path, limit=custom_limit) is True
    assert is_path_too_long(safe_path, limit=custom_limit) is False
    assert is_path_too_long("") is False

    # Test VirtualFilesystemTracker / VerificationEngine path length check simulation
    base_dir = os.path.abspath("/base/dir")
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
