import json
import os
import shutil
from unittest.mock import patch

import pytest

from app.core.db_conn import get_db_connection
from app.core.path_utils import safe_relpath


def test_safe_relpath_cross_drive():
    # Standard same drive test
    rel = safe_relpath(os.path.join("C:", "base", "sub", "file.txt"), os.path.join("C:", "base"))
    assert rel in ("sub/file.txt", "sub\\file.txt")

    # Mismatched Windows drive letters simulation
    def mock_splitdrive(path):
        p = str(path)
        if p.lower().startswith("d:"):
            return ("D:", p[2:])
        elif p.lower().startswith("c:"):
            return ("C:", p[2:])
        return ("", p)

    with patch("os.path.splitdrive", side_effect=mock_splitdrive):
        res = safe_relpath("D:\\external\\target\\file.txt", "C:\\base\\dir")
        assert res.lower().replace("\\", "/").endswith("d:/external/target/file.txt")


def test_session_target_dirs_registration(test_history_env):
    base_dir, db, cache, history_manager, db_worker = test_history_env

    # Create dummy snapshot
    session_id = history_manager.create_snapshot(base_dir)

    # Initially target_dirs should be empty
    sessions = history_manager.get_sessions()
    session = next(s for s in sessions if s["session_id"] == session_id)
    assert session.get("target_dirs") == []

    # Register external target dirs
    ext_dir_1 = os.path.join(base_dir, "ext_volume_1")
    ext_dir_2 = os.path.join(base_dir, "ext_volume_2")
    history_manager.register_target_dirs(session_id, [ext_dir_1, ext_dir_2])

    # Re-fetch sessions and verify target_dirs
    sessions = history_manager.get_sessions()
    session = next(s for s in sessions if s["session_id"] == session_id)
    assert os.path.normpath(ext_dir_1) in session["target_dirs"]
    assert os.path.normpath(ext_dir_2) in session["target_dirs"]


def test_check_missing_files_scans_external_targets(test_history_env, tmp_path):
    base_dir, db, cache, history_manager, db_worker = test_history_env

    # Create file in base_dir
    file_name = "test_doc.txt"
    file_src = os.path.join(base_dir, file_name)
    with open(file_src, "w") as f:
        f.write("content 123")

    session_id = history_manager.create_snapshot(base_dir)

    # Move file to external target directory
    ext_target_dir = str(tmp_path / "external_drive_target")
    os.makedirs(ext_target_dir, exist_ok=True)
    ext_file_path = os.path.join(ext_target_dir, file_name)
    shutil.move(file_src, ext_file_path)

    # Without target_dirs registered, check_missing_files reports file_name as missing
    missing = history_manager.check_missing_files(session_id)
    assert file_name in missing

    # Register external target dir
    history_manager.register_target_dirs(session_id, [ext_target_dir])

    # Now check_missing_files finds the file in ext_target_dir
    missing_after_reg = history_manager.check_missing_files(session_id)
    assert missing_after_reg == []

    # If external file is removed, missing file is reported again
    os.remove(ext_file_path)
    missing_after_remove = history_manager.check_missing_files(session_id)
    assert file_name in missing_after_remove


def test_cross_partition_undo_and_transactional_db_sync(test_history_env, tmp_path):
    base_dir, db, cache, history_manager, db_worker = test_history_env

    file_name = "sample.txt"
    file_src = os.path.join(base_dir, file_name)
    with open(file_src, "w") as f:
        f.write("sample document data")

    # Upsert into DB
    db.upsert_document(base_dir, file_name, "hash_sample", "text_sample")

    session_id = history_manager.create_snapshot(base_dir)

    # Move file across simulated boundary / external target
    ext_target_dir = str(tmp_path / "ext_partition")
    os.makedirs(ext_target_dir, exist_ok=True)
    ext_file_dst = os.path.join(ext_target_dir, file_name)
    shutil.move(file_src, ext_file_dst)

    # Update DB and register target_dir
    rel_dst = os.path.relpath(ext_file_dst, base_dir).replace("\\", "/")
    db.update_document_path(base_dir, file_name, rel_dst)
    history_manager.register_target_dirs(session_id, [ext_target_dir])

    # Verify missing files check passes
    assert history_manager.check_missing_files(session_id) == []

    # Perform rollback
    history_manager.rollback(session_id)

    # Physical restoration check
    assert os.path.exists(file_src)
    assert not os.path.exists(ext_file_dst)

    # DB state check
    conn = get_db_connection(db.db_path)
    with conn:
        cur = conn.execute("SELECT filepath FROM documents WHERE base_dir = ?", (base_dir,))
        filepaths = [r[0] for r in cur.fetchall()]
        assert file_name in filepaths
