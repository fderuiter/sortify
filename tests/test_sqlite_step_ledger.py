import asyncio
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings
from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_conn import get_db_connection
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager
from app.core.mover import execute_moves
from app.core.session import AppSession, scan_abandoned_sessions_async
from app.ui.app import AutoSorterApp


@pytest.fixture
def ledger_env(tmp_path):
    base_dir = str(tmp_path / "test_base")
    os.makedirs(base_dir, exist_ok=True)

    db_worker = DBWorker()
    db_path = tmp_path / "test_docs.db"
    db = Database(db_path, worker=db_worker)

    cache_path = tmp_path / "test_cache.db"
    cache = CacheManager(str(cache_path), worker=db_worker)

    history_manager = HistoryManager(db, cache, str(tmp_path / "test_history.db"))

    yield base_dir, db, cache, history_manager, db_worker
    db_worker.stop()


def test_realtime_logging_and_clearing_on_success(ledger_env):
    """Verify real-time step ledger logging and automatic cleanup on batch completion."""
    base_dir, db, cache, history_manager, db_worker = ledger_env

    file1 = os.path.join(base_dir, "doc1.txt")
    file2 = os.path.join(base_dir, "doc2.txt")
    with open(file1, "w") as f:
        f.write("content 1")
    with open(file2, "w") as f:
        f.write("content 2")

    db.upsert_document(base_dir, "doc1.txt", "hash1", "text1")
    db.upsert_document(base_dir, "doc2.txt", "hash2", "text2")

    plan = {
        "folder_a": {
            "doc1.txt": {
                "__type__": "file",
                "relative_source": "../doc1.txt",
                "status": "To Be Sorted",
            },
            "doc2.txt": {
                "__type__": "file",
                "relative_source": "../doc2.txt",
                "status": "To Be Sorted",
            },
        }
    }

    # Intercept log_step calls during execute_moves to verify real-time recording
    logged_steps = []
    orig_log_step = history_manager.log_step

    def spy_log_step(*args, **kwargs):
        step_id = orig_log_step(*args, **kwargs)
        logged_steps.append((args, kwargs, step_id))
        return step_id

    with patch.object(history_manager, "log_step", side_effect=spy_log_step):
        summary = execute_moves(base_dir, plan, db, history_manager)

    # 1. Verify two steps were logged in real time
    assert len(logged_steps) == 2

    # 2. Verify files moved
    assert os.path.exists(os.path.join(base_dir, "folder_a", "doc1.txt"))
    assert os.path.exists(os.path.join(base_dir, "folder_a", "doc2.txt"))

    # 3. Verify ledger entries cleared upon successful completion
    sessions = history_manager.get_sessions()
    session_id = sessions[0]["session_id"]
    ledger_entries = history_manager.get_step_ledger(session_id)
    assert len(ledger_entries) == 0


def test_bidirectional_path_mapping_and_collision_restoration(ledger_env):
    """Verify bidirectional path maps and pre-move filename restoration during collision unwind."""
    base_dir, db, cache, history_manager, db_worker = ledger_env

    # Setup source file and pre-existing target file to force collision
    src_file = os.path.join(base_dir, "report.pdf")
    with open(src_file, "w") as f:
        f.write("source report")

    target_dir = os.path.join(base_dir, "archive")
    os.makedirs(target_dir, exist_ok=True)

    # Pre-existing file at destination causing name collision
    colliding_file = os.path.join(target_dir, "report.pdf")
    with open(colliding_file, "w") as f:
        f.write("existing archive report")

    db.upsert_document(base_dir, "report.pdf", "hash_src", "source text")

    plan = {
        "archive": {
            "report.pdf": {
                "__type__": "file",
                "relative_source": "../report.pdf",
                "status": "To Be Sorted",
            }
        }
    }

    session_id = history_manager.create_snapshot(base_dir)

    # Execute move with collision
    summary = execute_moves(base_dir, plan, db, history_manager, resume=True)

    # The moved file should be renamed to report_1.pdf due to collision
    renamed_target = os.path.join(target_dir, "report_1.pdf")
    assert os.path.exists(renamed_target)
    assert os.path.exists(colliding_file)  # Original pre-existing file untouched

    # Test bidirectional path mapping retrieval
    # Manually log step to inspect get_bidirectional_path_map
    history_manager.log_step(
        session_id=session_id,
        source_path=src_file,
        target_path=renamed_target,
        original_filename="report.pdf",
        is_collision_renamed=True,
    )

    path_maps = history_manager.get_bidirectional_path_map(session_id)
    assert path_maps["forward"][src_file] == renamed_target
    assert path_maps["reverse"][renamed_target] == src_file
    assert renamed_target in path_maps["collisions"]
    assert path_maps["collisions"][renamed_target]["original_filename"] == "report.pdf"

    # Now unwind session
    history_manager.unwind_session(session_id, db=db)

    # Acceptance Criteria Check:
    # 1. Source file restored to original filename report.pdf
    assert os.path.exists(src_file)
    with open(src_file, "r") as f:
        assert f.read() == "source report"

    # 2. Renamed file report_1.pdf is deleted (no orphaned file in destination)
    assert not os.path.exists(renamed_target)

    # 3. Pre-existing colliding file report.pdf in target_dir remains untouched
    assert os.path.exists(colliding_file)
    with open(colliding_file, "r") as f:
        assert f.read() == "existing archive report"


def test_mid_execution_exception_reverse_unwinding(ledger_env):
    """Verify reverse step-by-step unwinding when batch move fails midway."""
    base_dir, db, cache, history_manager, db_worker = ledger_env

    f1 = os.path.join(base_dir, "f1.txt")
    f2 = os.path.join(base_dir, "f2.txt")
    f3 = os.path.join(base_dir, "f3.txt")
    for p, text in [(f1, "t1"), (f2, "text2"), (f3, "t3")]:
        with open(p, "w") as f:
            f.write(text)

    plan = {
        "dest": {
            "f1.txt": {
                "__type__": "file",
                "relative_source": "../f1.txt",
                "status": "To Be Sorted",
            },
            "f2.txt": {
                "__type__": "file",
                "relative_source": "../f2.txt",
                "status": "To Be Sorted",
            },
            "f3.txt": {
                "__type__": "file",
                "relative_source": "../f3.txt",
                "status": "To Be Sorted",
            },
        }
    }

    step_count = 0
    original_resilient_move = __import__(
        "app.core.resilient_file_ops"
    ).core.resilient_file_ops.resilient_move

    def failing_move(src, dst):
        nonlocal step_count
        step_count += 1
        if step_count == 3:  # Fail on 3rd file move
            raise OSError("Simulated disk error during move")
        return original_resilient_move(src, dst)

    with patch(
        "app.core.resilient_file_ops.resilient_move", side_effect=failing_move
    ):
        with pytest.raises(OSError, match="Simulated disk error during move"):
            execute_moves(base_dir, plan, db, history_manager)

    # Acceptance Criteria Check:
    # 1. All files returned to original location
    assert os.path.exists(f1)
    assert os.path.exists(f2)
    assert os.path.exists(f3)

    # 2. No orphaned files in destination directory
    dest_dir = os.path.join(base_dir, "dest")
    assert not os.path.exists(dest_dir) or not os.listdir(dest_dir)


def test_cross_volume_relocation_recording_and_unwind(ledger_env):
    """Verify cross-volume metadata logging and unwinding."""
    base_dir, db, cache, history_manager, db_worker = ledger_env

    src_file = os.path.join(base_dir, "data.csv")
    with open(src_file, "w") as f:
        f.write("col1,col2\nval1,val2")

    session_id = history_manager.create_snapshot(base_dir)

    target_file = os.path.join(base_dir, "out", "data.csv")

    # Mock _is_cross_volume to return True
    with patch("app.core.mover._is_cross_volume", return_value=True):
        history_manager.log_step(
            session_id=session_id,
            source_path=src_file,
            target_path=target_file,
            original_filename="data.csv",
            is_cross_volume=True,
            is_collision_renamed=False,
            file_hash="hash_csv",
        )

    ledger = history_manager.get_step_ledger(session_id)
    assert len(ledger) == 1
    assert ledger[0]["is_cross_volume"] is True

    # Move file physically to target
    os.makedirs(os.path.dirname(target_file), exist_ok=True)
    shutil.move(src_file, target_file)

    # Unwind cross-volume relocation
    unwound = history_manager.unwind_session(session_id, db=db)
    assert unwound == 1
    assert os.path.exists(src_file)
    assert not os.path.exists(target_file)


@pytest.fixture
def mock_session_base(tmp_path, monkeypatch):
    session_base = tmp_path / "autosorter_sessions"
    session_base.mkdir(parents=True, exist_ok=True)

    def mock_get_session_base_dir():
        return session_base

    monkeypatch.setattr(
        "app.core.path_utils.get_session_base_dir", mock_get_session_base_dir
    )
    return session_base


@pytest.mark.anyio
async def test_startup_recovery_triggers_uncommitted_batch_unwind(mock_session_base, tmp_path):
    """Verify that scan_abandoned_sessions_async detects uncommitted batch session with step ledger and triggers automatic recovery."""
    session_id = "uncommitted-session-123"
    session_dir = mock_session_base / session_id
    session_dir.mkdir()

    user_base = str(tmp_path / "user_files")
    os.makedirs(user_base, exist_ok=True)

    # Set up original file and moved file
    src_file = os.path.normpath(os.path.join(user_base, "uncommitted.txt"))
    target_file = os.path.normpath(os.path.join(user_base, "sorted", "uncommitted.txt"))
    os.makedirs(os.path.dirname(target_file), exist_ok=True)

    with open(target_file, "w") as f:
        f.write("uncommitted move content")

    # Create history.db inside session_dir using HistoryManager
    db_worker = DBWorker()
    docs_db = Database(tmp_path / "docs.db", worker=db_worker)
    cache = CacheManager(str(tmp_path / "cache.db"), worker=db_worker)
    history_db_path = str(session_dir / "history.db")
    history_mgr = HistoryManager(docs_db, cache, history_db_path)

    conn = get_db_connection(history_db_path)
    with conn:
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, 100.0, ?, 'active')",
            (session_id, user_base),
        )

    history_mgr.log_step(
        session_id=session_id,
        source_path=src_file,
        target_path=target_file,
        original_filename="uncommitted.txt",
    )

    # 1. scan_abandoned_sessions_async must detect uncommitted session
    abandoned = await scan_abandoned_sessions_async()
    assert len(abandoned) == 1
    assert abandoned[0]["session_id"] == session_id
    assert abandoned[0]["has_step_ledger"] is True
    assert abandoned[0]["uncommitted_batch"] is True

    # 2. Test UI check_abandoned_sessions triggers automatic recovery
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False

    app = AutoSorterApp(settings)

    created_tasks = []
    original_create_task = asyncio.create_task

    def mock_create_task(coro, *args, **kwargs):
        task = original_create_task(coro, *args, **kwargs)
        created_tasks.append(task)
        return task

    with patch("asyncio.create_task", side_effect=mock_create_task):
        app.check_abandoned_sessions()
        if created_tasks:
            await asyncio.gather(*created_tasks)

    # File must be restored to original location
    assert os.path.exists(src_file)
    assert not os.path.exists(target_file)

    db_worker.stop()
