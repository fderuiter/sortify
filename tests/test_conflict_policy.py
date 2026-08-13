import pytest
from pydantic import ValidationError

from app.config import Settings
from app.core.mover import _execute_moves_recursive


class DummyDB:
    def get_document(self, base_dir, source_rel_path):
        return None
    def update_document_path(self, base_dir, source_rel_path, rel_dest):
        pass

def test_conflict_policy_validation():
    # Valid values
    settings = Settings(CONFLICT_POLICY="rename")
    assert settings.CONFLICT_POLICY == "rename"

    settings = Settings(CONFLICT_POLICY="skip")
    assert settings.CONFLICT_POLICY == "skip"

    # Invalid values should raise ValidationError
    with pytest.raises(ValidationError):
        Settings(CONFLICT_POLICY="invalid")


def test_conflict_policy_skip(tmp_path):
    # Set up source and dest files
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    source_file = source_dir / "test.txt"
    source_file.write_text("source_content")

    # Collision destination file already exists with different content
    collision_file = dest_dir / "test.txt"
    collision_file.write_text("dest_content")

    plan = {
        "source/test.txt": {
            "__type__": "file",
            "relative_source": "source/test.txt",
            "target_filename": "test.txt",
            "status": "To Be Moved"
        }
    }

    class DummySettings:
        CONFLICT_POLICY = "skip"

    db = DummyDB()

    # Execute with "skip" policy
    _execute_moves_recursive(
        base_dir=str(tmp_path),
        plan=plan,
        db=db,
        current_dest="dest",
        runtime_settings=DummySettings()
    )

    # Since policy is skip, the source file should still exist and the destination file should not be modified
    assert source_file.exists()
    assert source_file.read_text() == "source_content"
    assert collision_file.exists()
    assert collision_file.read_text() == "dest_content"


def test_conflict_policy_rename(tmp_path):
    # Set up source and dest files
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    source_file = source_dir / "test.txt"
    source_file.write_text("source_content")

    # Collision destination file already exists with different content
    collision_file = dest_dir / "test.txt"
    collision_file.write_text("dest_content")

    plan = {
        "source/test.txt": {
            "__type__": "file",
            "relative_source": "source/test.txt",
            "target_filename": "test.txt",
            "status": "To Be Moved"
        }
    }

    class DummySettings:
        CONFLICT_POLICY = "rename"

    db = DummyDB()

    # Execute with "rename" policy
    _execute_moves_recursive(
        base_dir=str(tmp_path),
        plan=plan,
        db=db,
        current_dest="dest",
        runtime_settings=DummySettings()
    )

    # Since policy is rename, the source file should be moved, and a new suffix path created
    assert not source_file.exists()
    assert collision_file.exists()
    assert collision_file.read_text() == "dest_content"

    renamed_file = dest_dir / "test_1.txt"
    assert renamed_file.exists()
    assert renamed_file.read_text() == "source_content"
