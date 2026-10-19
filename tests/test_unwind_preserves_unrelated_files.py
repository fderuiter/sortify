"""Regression tests for #713: undo must leave files it didn't move alone."""

import os


def test_unwind_keeps_hidden_and_new_files(test_history_env):
    base_dir, db, _cache, history_manager, _worker = test_history_env

    os.makedirs(os.path.join(base_dir, ".git"))
    for rel, text in [
        (".git/config", "[core]"),
        (".env", "SECRET=1"),
        ("a.txt", "a"),
        ("b.txt", "b"),
    ]:
        with open(os.path.join(base_dir, rel), "w") as fh:
            fh.write(text)

    session_id = history_manager.create_snapshot(base_dir)
    os.makedirs(os.path.join(base_dir, "Cat"))
    src = os.path.join(base_dir, "a.txt")
    dst = os.path.join(base_dir, "Cat", "a.txt")
    os.rename(src, dst)
    history_manager.log_step(session_id, src, dst)

    with open(os.path.join(base_dir, "new_download.pdf"), "w") as fh:
        fh.write("user data")
    with open(os.path.join(base_dir, ".tmp_stage_deadbeef"), "w") as fh:
        fh.write("partial copy")

    history_manager.unwind_session(session_id, db=db)

    assert os.path.exists(os.path.join(base_dir, "a.txt"))
    assert os.path.exists(os.path.join(base_dir, ".git", "config"))
    assert os.path.exists(os.path.join(base_dir, ".env"))
    assert os.path.exists(os.path.join(base_dir, "new_download.pdf"))
    assert not os.path.exists(os.path.join(base_dir, ".tmp_stage_deadbeef"))
