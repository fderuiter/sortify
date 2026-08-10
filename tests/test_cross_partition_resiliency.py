import os
import shutil
from unittest.mock import patch

import pytest

from app.core.db_conn import get_db_connection
from app.core.mover import execute_moves


def test_single_file_cross_partition_move_failure(test_history_env):
    """
    Test scenario:
    - A single file cross-partition move where destination copy succeeds, but source deletion fails.
    - Expected behavior:
      - Raise exception.
      - Automatic rollback triggers.
      - Filesystem reverts to exact pre-move state (no leftover files/folders on target, source exists).
      - Database reverts to exact pre-move state.
    """
    base_dir, db, cache, history_manager, db_worker = test_history_env

    # 1. Create a single file
    file1_src = os.path.join(base_dir, "file1.txt")
    with open(file1_src, "w", newline="") as f:
        f.write("file1 content")

    # 2. Add document record to the database
    db.upsert_document(base_dir, "file1.txt", "hash1", "text1")

    # Verify pre-move state in DB
    conn = get_db_connection(db.db_path)
    with conn:
        cur = conn.execute(
            "SELECT filepath FROM documents WHERE base_dir = ?", (base_dir,)
        )
        initial_filepaths = {r[0] for r in cur.fetchall()}
    assert initial_filepaths == {"file1.txt"}

    # 3. Define the move plan
    target_dir = os.path.join(base_dir, "target_dir")
    file1_dst = os.path.join(target_dir, "file1.txt")

    plan = {
        "target_dir": {
            "file1.txt": {
                "__type__": "file",
                "relative_source": "../file1.txt",
                "target_filename": "file1.txt",
            }
        }
    }

    # 4. Mock shutil.move to simulate cross-partition deletion failure
    # It copies the file to destination, then throws PermissionError simulating deletion block.
    original_move = shutil.move

    def mock_move(src, dst):
        if src == file1_src and dst == file1_dst:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(src, "rb") as sf, open(dst, "wb") as df:
                df.write(sf.read())
            raise PermissionError("Simulated cross-partition source deletion failure.")
        return original_move(src, dst)

    # 5. Execute move and expect failure/rollback
    with patch("app.core.mover.shutil.move", side_effect=mock_move):
        with pytest.raises(
            PermissionError, match="Simulated cross-partition source deletion failure"
        ):
            execute_moves(base_dir, plan, db, history_manager)

    # 6. Verify filesystem boundaries
    import gc
    import time

    for i in range(20):
        gc.collect()
        try:
            # Source file must exist intact in original location
            assert os.path.exists(file1_src)
            with open(file1_src, "r", newline="") as f:
                assert f.read().strip() == "file1 content"

            # Destination file and folders must be cleaned up
            assert not os.path.exists(file1_dst)
            assert not os.path.exists(target_dir)
            break
        except AssertionError:
            if i == 19:
                raise
            time.sleep(0.1)

    # 7. Verify database records are restored to exact pre-move state
    conn = get_db_connection(db.db_path)
    with conn:
        cur = conn.execute(
            "SELECT filepath FROM documents WHERE base_dir = ?", (base_dir,)
        )
        final_filepaths = {r[0] for r in cur.fetchall()}
    assert final_filepaths == {"file1.txt"}


def test_batch_file_cross_partition_move_failure(test_history_env):
    """
    Test scenario:
    - A batch of files (File A and File B).
    - File A moves successfully (copied to target, source deleted).
    - File B fails during source deletion (copied to target, source remains).
    - Expected behavior:
      - The system rolls back the whole session.
      - File A is moved back to its source from target.
      - File B's target copy is cleaned up, and its source remains intact.
      - No leftover empty folders or duplicate files on target.
      - Database records are restored to exact pre-move state.
    """
    base_dir, db, cache, history_manager, db_worker = test_history_env

    # 1. Create two files
    fileA_src = os.path.join(base_dir, "fileA.txt")
    fileB_src = os.path.join(base_dir, "fileB.txt")
    with open(fileA_src, "w", newline="") as f:
        f.write("fileA content")
    with open(fileB_src, "w", newline="") as f:
        f.write("fileB content with distinct size for resiliency")

    # 2. Add records to the database
    db.upsert_document(base_dir, "fileA.txt", "hashA", "textA")
    db.upsert_document(base_dir, "fileB.txt", "hashB", "textB")

    # Verify initial database state
    conn = get_db_connection(db.db_path)
    with conn:
        cur = conn.execute(
            "SELECT filepath FROM documents WHERE base_dir = ?", (base_dir,)
        )
        initial_filepaths = {r[0] for r in cur.fetchall()}
    assert initial_filepaths == {"fileA.txt", "fileB.txt"}

    # 3. Define the move plan
    target_dir = os.path.join(base_dir, "target_dir")
    fileA_dst = os.path.join(target_dir, "fileA.txt")
    fileB_dst = os.path.join(target_dir, "fileB.txt")

    plan = {
        "target_dir": {
            "fileA.txt": {
                "__type__": "file",
                "relative_source": "../fileA.txt",
                "target_filename": "fileA.txt",
            },
            "fileB.txt": {
                "__type__": "file",
                "relative_source": "../fileB.txt",
                "target_filename": "fileB.txt",
            },
        }
    }

    # 4. Mock shutil.move
    # - fileA moves successfully.
    # - fileB fails after copy during source deletion.
    original_move = shutil.move

    def mock_move(src, dst):
        if src == fileB_src and dst == fileB_dst:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(src, "rb") as sf, open(dst, "wb") as df:
                df.write(sf.read())
            raise PermissionError(
                "Simulated batch element B cross-partition source deletion failure."
            )
        return original_move(src, dst)

    # 5. Execute move and expect failure/rollback
    with patch("app.core.mover.shutil.move", side_effect=mock_move):
        with pytest.raises(
            PermissionError,
            match="Simulated batch element B cross-partition source deletion failure",
        ):
            execute_moves(base_dir, plan, db, history_manager)

    # 6. Verify filesystem boundaries after rollback
    import gc
    import time

    for i in range(20):
        gc.collect()
        try:
            # Both files must exist at their original source locations with correct contents
            assert os.path.exists(fileA_src)
            with open(fileA_src, "r", newline="") as f:
                assert f.read().strip() == "fileA content"

            assert os.path.exists(fileB_src)
            with open(fileB_src, "r", newline="") as f:
                assert (
                    f.read().strip()
                    == "fileB content with distinct size for resiliency"
                )

            # Target directory and its files must be completely gone
            assert not os.path.exists(fileA_dst)
            assert not os.path.exists(fileB_dst)
            assert not os.path.exists(target_dir)
            break
        except AssertionError:
            if i == 19:
                raise
            time.sleep(0.1)

    # 7. Verify database records are restored to exact pre-move state
    conn = get_db_connection(db.db_path)
    with conn:
        cur = conn.execute(
            "SELECT filepath FROM documents WHERE base_dir = ?", (base_dir,)
        )
        final_filepaths = {r[0] for r in cur.fetchall()}
    assert final_filepaths == {"fileA.txt", "fileB.txt"}
