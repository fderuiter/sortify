import os
import time

from app.core.mover import _collect_move_items, _execute_moves_recursive


def test_collect_move_items_sorts_by_mtime(tmp_path):
    base_dir = str(tmp_path)

    # Create 3 files with explicit, different modification times
    f_recent = os.path.join(base_dir, "recent.txt")
    f_old = os.path.join(base_dir, "old.txt")
    f_archival = os.path.join(base_dir, "archival.txt")

    now = time.time()
    for p in (f_recent, f_old, f_archival):
        with open(p, "w") as f:
            f.write("test content")

    # Set mtimes: archival (10 days ago), old (5 days ago), recent (now)
    os.utime(f_archival, (now - 864000, now - 864000))
    os.utime(f_old, (now - 432000, now - 432000))
    os.utime(f_recent, (now, now))

    # Plan with keys out of order (recent first, archival last)
    plan = {
        "recent.txt": {"__type__": "file", "relative_source": "recent.txt", "target_filename": "moved_recent.txt"},
        "old.txt": {"__type__": "file", "relative_source": "old.txt", "target_filename": "moved_old.txt"},
        "archival.txt": {"__type__": "file", "relative_source": "archival.txt", "target_filename": "moved_archival.txt"},
    }

    items = _collect_move_items(base_dir, plan)

    # Verify collected items are sorted strictly ascending by file mtime
    assert len(items) == 3
    filenames = [item["filename"] for item in items]
    assert filenames == ["moved_archival.txt", "moved_old.txt", "moved_recent.txt"]


def test_recursive_move_execution_order_by_mtime(test_history_env):
    base_dir, db, cache, history_manager, db_worker = test_history_env

    f1 = os.path.join(base_dir, "new.txt")
    f2 = os.path.join(base_dir, "mid.txt")
    f3 = os.path.join(base_dir, "old.txt")

    now = time.time()
    for p in (f1, f2, f3):
        with open(p, "w") as f:
            f.write("content")

    os.utime(f3, (now - 1000, now - 1000))
    os.utime(f2, (now - 500, now - 500))
    os.utime(f1, (now, now))

    plan = {
        "new.txt": {"__type__": "file", "relative_source": "new.txt", "target_filename": "target_new.txt"},
        "mid.txt": {"__type__": "file", "relative_source": "mid.txt", "target_filename": "target_mid.txt"},
        "old.txt": {"__type__": "file", "relative_source": "old.txt", "target_filename": "target_old.txt"},
    }

    db_updates_batch = []
    _execute_moves_recursive(base_dir, plan, db, session_id="test-session", db_updates_batch=db_updates_batch)

    executed_sources = [item["args"][3] for item in db_updates_batch if item.get("type") == "transaction_step"]

    # Executed order must be old.txt -> mid.txt -> new.txt
    assert executed_sources == ["old.txt", "mid.txt", "new.txt"]
