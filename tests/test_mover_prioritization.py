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


def test_collect_move_items_age_tier_sorting(tmp_path):
    base_dir = str(tmp_path)
    now = time.time()
    day_sec = 86400

    f_new = os.path.join(base_dir, "new_file.txt")
    f_stale = os.path.join(base_dir, "stale_file.txt")
    f_archival = os.path.join(base_dir, "archival_file.txt")

    for p in (f_new, f_stale, f_archival):
        with open(p, "w") as f:
            f.write("content")

    # new (<30 days), stale (100 days), archival (400 days)
    os.utime(f_new, (now - 5 * day_sec, now - 5 * day_sec))
    os.utime(f_stale, (now - 100 * day_sec, now - 100 * day_sec))
    os.utime(f_archival, (now - 400 * day_sec, now - 400 * day_sec))

    plan = {
        "new_file.txt": {"__type__": "file", "relative_source": "new_file.txt", "target_filename": "dest_new.txt"},
        "stale_file.txt": {"__type__": "file", "relative_source": "stale_file.txt", "target_filename": "dest_stale.txt"},
        "archival_file.txt": {"__type__": "file", "relative_source": "archival_file.txt", "target_filename": "dest_archival.txt"},
    }

    items = _collect_move_items(base_dir, plan, priority="age_tier")
    assert len(items) == 3
    filenames = [item["filename"] for item in items]
    assert filenames == ["dest_archival.txt", "dest_stale.txt", "dest_new.txt"]


def test_async_move_engine_executes_priority_chunks_first(test_history_env):
    from app.core.mover import AsyncMoveEngine

    base_dir, db, cache, history_manager, db_worker = test_history_env
    now = time.time()
    day_sec = 86400

    f_recent = os.path.join(base_dir, "recent_doc.txt")
    f_archival = os.path.join(base_dir, "archival_doc.txt")

    for p in (f_recent, f_archival):
        with open(p, "w") as f:
            f.write("content")

    os.utime(f_recent, (now, now))
    os.utime(f_archival, (now - 500 * day_sec, now - 500 * day_sec))

    plan = {
        "recent_doc.txt": {
            "__type__": "file",
            "relative_source": "recent_doc.txt",
            "target_filename": "out_recent.txt",
            "user_confirmed": True,
            "status": "confirmed",
        },
        "archival_doc.txt": {
            "__type__": "file",
            "relative_source": "archival_doc.txt",
            "target_filename": "out_archival.txt",
            "user_confirmed": True,
            "status": "confirmed",
        },
    }

    engine = AsyncMoveEngine(max_workers=1, chunk_size=1)
    summary = engine.execute(
        base_dir=base_dir,
        plan=plan,
        db=db,
        history_manager=history_manager,
        priority="age_tier",
    )

    assert os.path.exists(os.path.join(base_dir, "out_archival.txt"))
    assert os.path.exists(os.path.join(base_dir, "out_recent.txt"))

