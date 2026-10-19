"""Regression tests for #715: undo must not delete a moved file when its old path is taken."""

import os


def _sort_one_file(base_dir, history_manager, name="report.txt", content="ORIGINAL"):
    src = os.path.join(base_dir, name)
    with open(src, "w") as fh:
        fh.write(content)
    session_id = history_manager.create_snapshot(base_dir)
    dst_dir = os.path.join(base_dir, "Cat")
    os.makedirs(dst_dir)
    dst = os.path.join(dst_dir, name)
    os.rename(src, dst)
    history_manager.log_step(session_id, src, dst)
    return session_id, src, dst


def test_unwind_keeps_moved_file_when_original_path_is_occupied(test_history_env):
    base_dir, db, _cache, history_manager, _worker = test_history_env
    session_id, src, _dst = _sort_one_file(base_dir, history_manager)

    with open(src, "w") as fh:
        fh.write("unrelated NEW file")

    history_manager.unwind_session(session_id, db=db)

    with open(src) as fh:
        assert fh.read() == "unrelated NEW file"
    restored = os.path.join(base_dir, "report_1.txt")
    assert os.path.exists(restored)
    with open(restored) as fh:
        assert fh.read() == "ORIGINAL"


def test_unwind_drops_identical_duplicate(test_history_env):
    base_dir, db, _cache, history_manager, _worker = test_history_env
    session_id, src, dst = _sort_one_file(base_dir, history_manager)

    with open(src, "w") as fh:
        fh.write("ORIGINAL")

    history_manager.unwind_session(session_id, db=db)

    with open(src) as fh:
        assert fh.read() == "ORIGINAL"
    assert not os.path.exists(dst)
    assert sorted(os.listdir(base_dir)) == ["report.txt"]
