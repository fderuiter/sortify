from unittest import mock

from app.config import Settings
from app.core.mover import _remove_empty_dirs, execute_moves


def test_remove_empty_dirs_respects_protected_paths(tmp_path):
    # Setup directories
    # tmp_path/empty_dir (protected)
    # tmp_path/empty_dir/nested (nested within protected)
    # tmp_path/other_empty_dir (unprotected)

    empty_dir = tmp_path / "empty_dir"
    nested_dir = empty_dir / "nested"
    other_empty_dir = tmp_path / "other_empty_dir"

    nested_dir.mkdir(parents=True, exist_ok=True)
    other_empty_dir.mkdir(parents=True, exist_ok=True)

    protected_paths = [str(empty_dir)]

    # Run cleanup starting at the root (tmp_path)
    # Since we start above empty_dir, it should recurse down, hit empty_dir,
    # find that empty_dir is in protected_paths, and return immediately,
    # leaving empty_dir and nested_dir intact, but other_empty_dir should be deleted.
    _remove_empty_dirs(str(tmp_path), protected_paths)

    assert empty_dir.exists()
    assert nested_dir.exists()
    assert not other_empty_dir.exists()


def test_remove_empty_dirs_stop_at_protected_boundary(tmp_path):
    # Setup nested hierarchy:
    # tmp_path/parent (protected)
    # tmp_path/parent/child (nested)
    # tmp_path/parent/child/grandchild (nested)

    parent = tmp_path / "parent"
    child = parent / "child"
    grandchild = child / "grandchild"

    grandchild.mkdir(parents=True, exist_ok=True)

    protected_paths = [str(parent)]

    # Let's call _remove_empty_dirs starting at grandchild directly - since parent is protected and grandchild is a subpath,
    # it should return immediately and not delete grandchild.
    _remove_empty_dirs(str(grandchild), protected_paths)
    assert grandchild.exists()

    # Call on the whole tree
    _remove_empty_dirs(str(tmp_path), protected_paths)
    assert parent.exists()
    assert child.exists()
    assert grandchild.exists()


def test_execute_moves_empty_protected_paths(tmp_path):
    # Verify that everything operates successfully without error when protected list is empty
    mock_db = mock.MagicMock()
    mock_history = mock.MagicMock()

    base_dir = tmp_path / "base"
    base_dir.mkdir()

    d1 = base_dir / "d1"
    d1.mkdir()

    plan = {
        "d1": {
            "__type__": "directory",
            "source_path": str(d1),
            "status": "To Be Deleted",
            "protected": False,
        }
    }

    settings = Settings(PROTECTED_PATHS=[])

    execute_moves(
        base_dir=str(base_dir),
        plan=plan,
        db=mock_db,
        history_manager=mock_history,
        runtime_settings=settings,
    )

    # d1 is not protected and is empty, so it should be deleted
    assert not d1.exists()


def test_execute_moves_with_protected_paths(tmp_path):
    # Verify that execute_moves retrieves protected paths and protects matched folders in dirs_to_process and cleanup sweep
    mock_db = mock.MagicMock()
    mock_history = mock.MagicMock()

    base_dir = tmp_path / "base"
    base_dir.mkdir()

    d1 = base_dir / "d1"
    d2 = base_dir / "d2"
    d1.mkdir()
    d2.mkdir()

    plan = {
        "d1": {
            "__type__": "directory",
            "source_path": str(d1),
            "status": "To Be Deleted",
            "protected": False,
        },
        "d2": {
            "__type__": "directory",
            "source_path": str(d2),
            "status": "To Be Deleted",
            "protected": False,
        },
    }

    # Protect d1
    settings = Settings(PROTECTED_PATHS=[str(d1)])

    summary = execute_moves(
        base_dir=str(base_dir),
        plan=plan,
        db=mock_db,
        history_manager=mock_history,
        runtime_settings=settings,
    )

    # d1 should be protected and NOT deleted
    assert d1.exists()
    # d2 is not protected and empty, so it should be deleted
    assert not d2.exists()
    # Check that summary correctly reflects protected and deleted folders
    assert summary["protected_folders"] == 1
    assert summary["deleted_folders"] == 1


def test_is_subpath_or_equal():
    from app.core.mover import is_subpath_or_equal

    assert is_subpath_or_equal("/app/dir", "/app/dir") is True
    assert is_subpath_or_equal("/app/dir/sub", "/app/dir") is True
    assert is_subpath_or_equal("/app/dir2", "/app/dir") is False
    assert is_subpath_or_equal("/app/dir/sub", "/app/dir/sub/nested") is False
    assert is_subpath_or_equal(None, "/app/dir") is False
    assert is_subpath_or_equal("/app/dir", None) is False

    # Test case-insensitivity which is important on Windows and macOS if file system is case-insensitive
    import sys

    if sys.platform == "win32":
        assert is_subpath_or_equal("/APP/DIR/SUB", "/app/dir") is True
        assert is_subpath_or_equal("/app/dir/sub", "/APP/DIR") is True
