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


def test_check_ai_status_florence2_integrity_failure(tmp_path, monkeypatch):
    """Verify check_ai_status detects Florence-2 integrity failures."""
    from unittest.mock import patch
    import pytest
    from app.config import AppSettings
    from app.core.shared_registry import SharedModelRegistry
    from app.core.verifier import check_ai_status

    settings = AppSettings()
    settings.AI_ASSISTED_NAMING = True

    # Register expected hashes for florence-2
    SharedModelRegistry._instance = None
    registry = SharedModelRegistry.get_instance()
    registry.register_expected_hashes("florence-2", {"config.json": "expected_hash"})

    # Setup florence-2 path
    f2_dir = tmp_path / "florence-2"
    f2_dir.mkdir()
    (f2_dir / "config.json").write_bytes(b"tampered config")

    monkeypatch.setattr(
        "app.core.offline_loader.OfflineModelLoader.resolve_model_path",
        lambda model_id: str(f2_dir) if model_id == "florence-2" else str(tmp_path),
    )

    def mock_verify_integrity(model_id, path):
        if model_id == "florence-2":
            raise ValueError("Integrity check failed for config.json")
        return True

    # Case 1: Non-sandboxed mode -> returns healthy=False with warning
    with (
        patch("app.core.verifier.is_ml_available", return_value=True),
        patch("os.path.exists", return_value=True),
        patch.object(registry, "verify_integrity", side_effect=mock_verify_integrity),
    ):
        is_healthy, warn_msg = check_ai_status(settings)
        assert is_healthy is False
        assert "Florence-2 vision model integrity check failed" in warn_msg

    # Case 2: Sandboxed mode -> raises ValueError
    with (
        patch("app.core.verifier.is_ml_available", return_value=True),
        patch("os.path.exists", return_value=True),
        patch("app.core.path_utils.is_packaged", return_value=True),
        patch.object(registry, "verify_integrity", side_effect=mock_verify_integrity),
    ):
        with pytest.raises(
            ValueError, match="Florence-2 vision model integrity check failed"
        ):
            check_ai_status(settings)
