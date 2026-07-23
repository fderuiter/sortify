import os

from app.core.verifier import VerificationEngine


def test_get_moves_flattens_plan():
    base_dir = "/base/dir"
    plan = {
        "file1.txt": None,
        "folder1": {
            "file2.txt": {"__type__": "file", "target_filename": "renamed2.txt"},
            "subfolder": {"file3.txt": None},
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
        os.path.join(base_dir, "file2.txt"),
        os.path.join(base_dir, "folder1", "renamed2.txt"),
    )

    # Check file3.txt
    assert moves[2] == (
        "file3.txt",
        os.path.join(base_dir, "file3.txt"),
        os.path.join(base_dir, "folder1", "subfolder", "file3.txt"),
    )
