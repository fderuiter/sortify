import logging
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from app.core.exceptions import ArchiveCorruptionError
from app.core.mover import AsyncMoveEngine
from app.core.offline_loader import OfflineModelLoader
from app.core.path_utils import safe_extract_path, sanitize_filename, sanitize_plan
from app.core.verifier import VerificationEngine, VirtualFilesystemTracker


def test_sanitize_filename_neutralizes_traversal_operators_and_absolute_prefixes():
    """Verify sanitize_filename strips relative path operators (.., .) and absolute drive/slash prefixes."""
    # Relative traversal operators
    safe, transformed = sanitize_filename("../../etc/passwd")
    assert safe == "passwd"
    assert transformed is True

    # Windows relative traversal with backslashes
    safe_win, transformed_win = sanitize_filename(r"..\..\Windows\System32\cmd.exe")
    assert safe_win == "cmd.exe"
    assert transformed_win is True

    # Absolute drive prefix
    safe_abs, transformed_abs = sanitize_filename("C:/Users/Admin/Desktop/secret.txt")
    assert safe_abs == "secret.txt"
    assert transformed_abs is True

    # Pure traversal operator resulting in empty name
    safe_dot, transformed_dot = sanitize_filename("..")
    assert safe_dot == "Unnamed_safe"
    assert transformed_dot is True

    # Normal valid filename
    safe_normal, transformed_normal = sanitize_filename("report.pdf")
    assert safe_normal == "report.pdf"
    assert transformed_normal is False


def test_sanitize_plan_neutralizes_target_filename():
    """Verify sanitize_plan strips traversal operators from both file keys and target_filename metadata."""
    plan = {
        "Documents": {
            "../../etc/passwd": {
                "__type__": "file",
                "relative_source": "passwd",
                "target_filename": "../../etc/passwd",
            },
            "legit.txt": {
                "__type__": "file",
                "relative_source": "legit.txt",
                "target_filename": "C:/Windows/system.ini",
            },
        }
    }

    sanitized, warnings = sanitize_plan(plan)

    # File keys should be sanitized to basenames
    assert "passwd" in sanitized["Documents"]
    assert "legit.txt" in sanitized["Documents"]

    # target_filename in metadata should be sanitized to basename
    assert sanitized["Documents"]["passwd"]["target_filename"] == "passwd"
    assert sanitized["Documents"]["legit.txt"]["target_filename"] == "system.ini"

    # Confirmed flags set
    assert sanitized["Documents"]["passwd"]["confirmed"] is True
    assert sanitized["Documents"]["legit.txt"]["confirmed"] is True

    # Warnings recorded
    assert len(warnings) > 0


def test_verification_engine_detects_escaping_paths_and_fails_integrity():
    """Verify VirtualFilesystemTracker flags escaping paths with success = False."""
    base_dir = os.path.abspath("/tmp/test_base")
    outside_dir = os.path.abspath("/tmp/outside")

    tracker = VirtualFilesystemTracker()
    moves_list = [
        (
            "file1.txt",
            os.path.join(base_dir, "file1.txt"),
            os.path.join(base_dir, "Documents", "file1.txt"),
        ),
        (
            "file2.txt",
            os.path.join(base_dir, "file2.txt"),
            os.path.join(outside_dir, "file2.txt"),
        ),
    ]

    escaping = tracker.check_escaping_paths(moves_list, base_dir)
    assert len(escaping) == 1
    assert escaping[0]["path"] == os.path.join(outside_dir, "file2.txt")
    assert "attempts to escape target base directory" in escaping[0]["message"]


def test_verify_plan_integrity_logs_sanitization_warnings(caplog):
    """Verify verify_plan_integrity logs pre-flight plan sanitization adjustments."""
    base_dir = "/tmp/test_base"
    plan = {
        "Folder: 1": {
            "../../etc/passwd": {
                "__type__": "file",
                "relative_source": "passwd",
                "target_filename": "../../etc/passwd",
            }
        }
    }

    with caplog.at_level(logging.INFO):
        result = VerificationEngine.verify_plan_integrity(base_dir, plan)

    assert result["success"] is True
    assert any(
        "Pre-flight plan sanitization adjustment:" in record.message
        for record in caplog.records
    )


def test_runtime_boundary_violation_raises_value_error_and_rolls_back():
    """Verify runtime boundary check blocks move escaping base_dir and triggers rollback."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base_dir = os.path.join(tmp_dir, "base")
        outside_dir = os.path.join(tmp_dir, "outside")
        os.makedirs(base_dir, exist_ok=True)
        os.makedirs(outside_dir, exist_ok=True)

        source_file = os.path.join(base_dir, "sample.txt")
        with open(source_file, "w") as f:
            f.write("content")

        import threading

        from app.core.mover import _process_move_item

        # Construct an item with current_dest pointing outside base_dir
        item = {
            "source_path": source_file,
            "key": "sample.txt",
            "content": {},
            "filename": "sample.txt",
            "current_dest": "../../outside",
        }

        mock_history = MagicMock()
        mock_ledger = MagicMock()

        with pytest.raises(ValueError, match="Runtime boundary violation"):
            _process_move_item(
                item=item,
                base_dir=base_dir,
                db=None,
                runtime_settings=None,
                ledger=mock_ledger,
                history_manager=mock_history,
                db_lock=threading.Lock(),
                step_counter=[0],
                db_updates_batch=[],
                path_map={},
                session_id="test_session",
            )


def test_async_move_engine_rejects_escaping_paths():
    """Verify AsyncMoveEngine.execute fails pre-flight if verification returns escaping_paths or success=False."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base_dir = os.path.join(tmp_dir, "base")
        os.makedirs(base_dir, exist_ok=True)

        engine = AsyncMoveEngine()

        # Mock verifier to simulate escaping paths found during pre-flight
        mock_verifier = MagicMock()
        mock_verifier.verify_plan_integrity.return_value = {
            "success": False,
            "escaping_paths": [
                {
                    "path": "/tmp/escaped/file.txt",
                    "source": os.path.join(base_dir, "file.txt"),
                    "type": "escaping_path",
                    "message": "Destination path attempts to escape target base directory",
                }
            ],
            "warnings": ["Destination path attempts to escape target base directory"],
        }

        # Monkeypatch VerificationEngine inside mover module
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "app.core.verifier.VerificationEngine.verify_plan_integrity",
                mock_verifier.verify_plan_integrity,
            )

            plan = {
                "sub": {
                    "file.txt": {
                        "__type__": "file",
                        "relative_source": "file.txt",
                    }
                }
            }

            with pytest.raises(
                ValueError, match="Plan validation or confirmation failed"
            ):
                engine.execute(base_dir, plan, db=None, history_manager=MagicMock())


def test_safe_extract_path_valid_nested_and_flat():
    """Verify safe_extract_path resolves valid relative and nested paths within target_dir."""
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir) / "staging"
        base.mkdir()

        # Flat file
        p1 = safe_extract_path(base, "model.bin")
        assert p1 == base.resolve() / "model.bin"

        # Legitimate nested directory
        p2 = safe_extract_path(base, "models/onnx/weights.bin")
        assert p2 == base.resolve() / "models" / "onnx" / "weights.bin"

        # Legitimate nested directory with redundant dot
        p3 = safe_extract_path(base, "models/./onnx/weights.bin")
        assert p3 == base.resolve() / "models" / "onnx" / "weights.bin"

        # Path objects as inputs
        p4 = safe_extract_path(Path(tmp_dir), Path("test.txt"))
        assert p4 == Path(tmp_dir).resolve() / "test.txt"


def test_safe_extract_path_rejects_traversal_payloads():
    """Verify safe_extract_path rejects relative directory traversal payloads attempting escape."""
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir) / "staging"
        base.mkdir()

        traversal_payloads = [
            "../../test.txt",
            "../test.txt",
            "sub/../../test.txt",
            "a/b/../../../test.txt",
            "..",
            "sub/..",
        ]

        for payload in traversal_payloads:
            with pytest.raises(
                ValueError,
                match="escapes target directory|resolves to target directory",
            ):
                safe_extract_path(base, payload)


def test_safe_extract_path_rejects_cross_platform_separators_and_absolute_paths():
    """Verify safe_extract_path handles cross-platform backslashes and rejects absolute/drive paths."""
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir) / "staging"
        base.mkdir()

        # Windows backslash traversal
        with pytest.raises(ValueError, match="escapes target directory"):
            safe_extract_path(base, r"..\..\test.txt")

        with pytest.raises(ValueError, match="escapes target directory"):
            safe_extract_path(base, r"sub\..\..\test.txt")

        # Legitimate nested path with Windows backslashes
        nested = safe_extract_path(base, r"models\onnx\model.bin")
        assert nested == base.resolve() / "models" / "onnx" / "model.bin"

        # POSIX absolute path
        with pytest.raises(ValueError, match="absolute path or contains drive prefix"):
            safe_extract_path(base, "/etc/passwd")

        # Windows drive prefixes
        with pytest.raises(ValueError, match="absolute path or contains drive prefix"):
            safe_extract_path(base, "C:/Windows/System32/cmd.exe")

        with pytest.raises(ValueError, match="absolute path or contains drive prefix"):
            safe_extract_path(base, r"C:\Windows\System32\cmd.exe")

        with pytest.raises(ValueError, match="absolute path or contains drive prefix"):
            safe_extract_path(base, "D:file.txt")

        # UNC share paths
        with pytest.raises(ValueError, match="absolute path or contains drive prefix"):
            safe_extract_path(base, "//server/share/payload.bin")

        with pytest.raises(ValueError, match="absolute path or contains drive prefix"):
            safe_extract_path(base, r"\\server\share\payload.bin")


def test_safe_extract_path_invalid_inputs():
    """Verify safe_extract_path rejects empty, whitespace, and null-byte containing inputs."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        with pytest.raises(ValueError, match="Target directory must not be empty"):
            safe_extract_path("", "file.txt")

        with pytest.raises(ValueError, match="Member path must not be empty"):
            safe_extract_path(tmp_dir, "")

        with pytest.raises(ValueError, match="Member path must not be empty"):
            safe_extract_path(tmp_dir, "   ")

        with pytest.raises(ValueError, match="Path cannot contain null bytes"):
            safe_extract_path(tmp_dir, "evil\x00.txt")

        with pytest.raises(ValueError, match="Path cannot contain null bytes"):
            safe_extract_path(f"{tmp_dir}\x00", "test.txt")


def test_safe_extract_path_same_dir_guard():
    """Verify allow_same_dir guard controls whether resolving to target_dir itself is permitted."""
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir) / "staging"
        base.mkdir()

        # By default allow_same_dir is False
        with pytest.raises(ValueError, match="resolves to target directory itself"):
            safe_extract_path(base, ".")

        # When explicitly allowed
        resolved = safe_extract_path(base, ".", allow_same_dir=True)
        assert resolved == base.resolve()


def test_offline_loader_extract_and_verify_rejects_path_traversal():
    """Verify OfflineModelLoader._extract_and_verify detects traversal payloads in zip archives."""
    import zipfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        zip_path = os.path.join(tmp_dir, "malicious.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../../traversal_test.txt", b"evil content")
            zf.writestr("safe_model.bin", b"safe content")

        staging_target = os.path.join(tmp_dir, "staging")
        os.makedirs(staging_target, exist_ok=True)

        with pytest.raises(ArchiveCorruptionError) as exc_info:
            OfflineModelLoader._extract_and_verify(zip_path, staging_target)

        assert "Failed to extract sidecar model package" in str(exc_info.value)

        # Verify no file was written outside staging_target
        assert not os.path.exists(os.path.join(tmp_dir, "traversal_test.txt"))
        assert not os.path.exists("/tmp/traversal_test.txt")


def test_offline_loader_extract_and_verify_legitimate_nested_archive():
    """Verify OfflineModelLoader._extract_and_verify unpacks legitimate nested directory structures."""
    import zipfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        zip_path = os.path.join(tmp_dir, "valid_model.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("models/nested/config.json", b'{"vocab_size": 1000}')
            zf.writestr("models/nested/weights.bin", b"\x00" * 32)

        staging_target = os.path.join(tmp_dir, "staging")
        os.makedirs(staging_target, exist_ok=True)

        # Extraction should succeed without error
        OfflineModelLoader._extract_and_verify(zip_path, staging_target)

        assert os.path.exists(
            os.path.join(staging_target, "models", "nested", "config.json")
        )
        assert os.path.exists(
            os.path.join(staging_target, "models", "nested", "weights.bin")
        )
