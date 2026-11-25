from unittest import mock

from app.config import Settings
from app.core.mover import execute_moves


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
    assert is_subpath_or_equal("/app/dir/", "/app/dir") is True
    assert is_subpath_or_equal("/app/dir", "/app/dir/") is True
    assert is_subpath_or_equal("/app/dir/sub", "/app/dir") is True
    assert is_subpath_or_equal("/app/dir/sub", "/app/dir/") is True
    assert is_subpath_or_equal("/app/dir2", "/app/dir") is False
    assert is_subpath_or_equal("/app/dir/sub", "/app/dir/sub/nested") is False
    assert is_subpath_or_equal(None, "/app/dir") is False
    assert is_subpath_or_equal("/app/dir", None) is False

    # Root path checks
    assert is_subpath_or_equal("/app", "/") is True
    assert is_subpath_or_equal("/", "/") is True

    # Test case-insensitivity which is important on Windows and macOS if file system is case-insensitive
    import sys

    if sys.platform == "win32":
        assert is_subpath_or_equal("/APP/DIR/SUB", "/app/dir") is True
        assert is_subpath_or_equal("/app/dir/sub", "/APP/DIR") is True
        assert is_subpath_or_equal("C:\\foo\\bar", "C:\\") is True
        assert is_subpath_or_equal("C:\\foo\\bar", "C:\\foo\\") is True
        assert is_subpath_or_equal("C:\\foo", "C:\\foo\\") is True
        assert is_subpath_or_equal("C:\\foo\\", "C:\\foo") is True
