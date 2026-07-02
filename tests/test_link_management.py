import os

from app.core.link_manager import LinkManager
from app.core.mover import execute_moves
from app.core.scanner import get_files_recursively

try:
    import pylnk3
except ImportError:
    pylnk3 = None


def test_relative_symlink_update(tmp_path):
    # Setup
    base_dir = str(tmp_path)

    # Create target file
    target_dir = os.path.join(base_dir, "target_dir")
    os.makedirs(target_dir)
    target_file = os.path.join(target_dir, "data.txt")
    with open(target_file, "w") as f:
        f.write("content")

    # Create relative symlink in root
    symlink_path = os.path.join(base_dir, "link.txt")
    os.symlink("target_dir/data.txt", symlink_path)

    # Scan files to register links
    get_files_recursively(base_dir)
    assert LinkManager.get_link_info(symlink_path) is not None

    # We want to move link.txt into deeper_dir/link.txt
    plan = {
        "deeper_dir": {
            "link.txt": {
                "__type__": "file",
                "status": "Pending Move",
                "source_path": "link.txt",
                "target_filename": "link.txt",
            }
        },
        "target_dir": {"data.txt": None},
    }

    execute_moves(base_dir, plan)

    # Verify link was moved and updated
    new_symlink_path = os.path.join(base_dir, "deeper_dir", "link.txt")
    assert os.path.exists(new_symlink_path)
    assert os.path.islink(new_symlink_path)

    # Check new target
    new_target = os.readlink(new_symlink_path)
    assert new_target == os.path.join("..", "target_dir", "data.txt")
