import shutil

import pytest
from textual.widgets import Button, DataTable, Input, Label

from app.config import AppSettings
from app.core.db_worker import DBWorker
from app.core.pipeline import SortPipeline
from app.core.store import open_store
from app.ui.modals.session import RollbackConfirmModal
from app.ui.tui import AutoSorterTUI


@pytest.fixture
def selective_env(tmp_path, monkeypatch):
    """Fixture providing a test environment with a sorting session of 3 files."""
    base_dir = tmp_path / "workspace"
    base_dir.mkdir()

    file1 = base_dir / "file1.txt"
    file2 = base_dir / "file2.txt"
    file3 = base_dir / "file3.txt"
    file1.write_text("file 1 content")
    file2.write_text("file 2 content")
    file3.write_text("file 3 content")

    app_dir = tmp_path / "app"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    monkeypatch.setattr(
        "app.core.session_catalog.find_abandoned_sessions", lambda *a, **k: []
    )

    store = open_store(app_dir, legacy_session_root=tmp_path / "none")
    db_worker = DBWorker()
    autosorter_db = store.database(db_worker)
    history_mgr = store.history(autosorter_db, store.cache(db_worker))

    session_id = history_mgr.create_snapshot(str(base_dir))

    target_dir = base_dir / "Sorted"
    target_dir.mkdir()

    target1 = target_dir / "file1.txt"
    target2 = target_dir / "file2.txt"
    target3 = target_dir / "file3.txt"

    shutil.move(str(file1), str(target1))
    shutil.move(str(file2), str(target2))
    shutil.move(str(file3), str(target3))

    step1 = history_mgr.log_step(
        session_id=session_id,
        source_path=str(file1),
        target_path=str(target1),
        original_path=str(file1),
        original_filename="file1.txt",
    )
    step2 = history_mgr.log_step(
        session_id=session_id,
        source_path=str(file2),
        target_path=str(target2),
        original_path=str(file2),
        original_filename="file2.txt",
    )
    step3 = history_mgr.log_step(
        session_id=session_id,
        source_path=str(file3),
        target_path=str(target3),
        original_path=str(file3),
        original_filename="file3.txt",
    )

    yield {
        "tmp_path": tmp_path,
        "base_dir": base_dir,
        "target_dir": target_dir,
        "file1": file1,
        "file2": file2,
        "file3": file3,
        "target1": target1,
        "target2": target2,
        "target3": target3,
        "session_id": session_id,
        "step1": step1,
        "step2": step2,
        "step3": step3,
        "history_mgr": history_mgr,
        "store": store,
        "db_worker": db_worker,
    }

    db_worker.stop(stop_embeddings=False)


def test_get_session_moves(selective_env):
    history_mgr = selective_env["history_mgr"]
    session_id = selective_env["session_id"]

    moves = history_mgr.get_session_moves(session_id)
    assert len(moves) == 3
    step_ids = [m["step_id"] for m in moves]
    assert selective_env["step1"] in step_ids
    assert selective_env["step2"] in step_ids
    assert selective_env["step3"] in step_ids

    for m in moves:
        assert "source_path" in m
        assert "target_path" in m
        assert "status" in m


def test_selective_rollback_execution_and_status(selective_env):
    history_mgr = selective_env["history_mgr"]
    session_id = selective_env["session_id"]
    step1 = selective_env["step1"]
    step2 = selective_env["step2"]
    step3 = selective_env["step3"]

    file1 = selective_env["file1"]
    file2 = selective_env["file2"]
    file3 = selective_env["file3"]
    target1 = selective_env["target1"]
    target2 = selective_env["target2"]
    target3 = selective_env["target3"]

    # Revert only step1
    restored = history_mgr.rollback(session_id, selected_step_ids=[step1])
    assert restored == 1

    # file1 restored, file2/file3 still in target
    assert file1.exists()
    assert not target1.exists()
    assert not file2.exists()
    assert target2.exists()
    assert not file3.exists()
    assert target3.exists()

    # Session status is partially_rolled_back
    sessions = history_mgr.get_sessions()
    sess = next(s for s in sessions if s["session_id"] == session_id)
    assert sess["status"] == "partially_rolled_back"

    # Remaining moves list contains step2 and step3
    remaining_moves = history_mgr.get_session_moves(session_id)
    assert len(remaining_moves) == 2
    rem_ids = [m["step_id"] for m in remaining_moves]
    assert step2 in rem_ids
    assert step3 in rem_ids

    # Revert remaining steps
    restored2 = history_mgr.rollback(session_id, selected_step_ids=[step2, step3])
    assert restored2 == 2

    assert file2.exists()
    assert not target2.exists()
    assert file3.exists()
    assert not target3.exists()

    sessions = history_mgr.get_sessions()
    sess = next(s for s in sessions if s["session_id"] == session_id)
    assert sess["status"] == "rolled_back"


def test_selective_rollback_creates_safety_snapshot(selective_env):
    history_mgr = selective_env["history_mgr"]
    session_id = selective_env["session_id"]
    step1 = selective_env["step1"]

    before_sessions = len(history_mgr.get_sessions())
    history_mgr.rollback(session_id, selected_step_ids=[step1])
    after_sessions = len(history_mgr.get_sessions())

    # Safety snapshot added a session
    assert after_sessions > before_sessions


def test_sort_pipeline_undo_selective(selective_env):
    store = selective_env["store"]
    db_worker = selective_env["db_worker"]
    session_id = selective_env["session_id"]
    step1 = selective_env["step1"]

    class DummySession:
        def __init__(self, history):
            self.history_manager = history
            self.settings = AppSettings()

    pipeline = SortPipeline(session=DummySession(selective_env["history_mgr"]))
    res = pipeline.undo(session_id, selected_step_ids=[step1])

    assert res.summary["restored"] == 1
    assert selective_env["file1"].exists()


@pytest.mark.anyio
async def test_rollback_confirm_modal_ui(selective_env):
    session_id = selective_env["session_id"]
    history_mgr = selective_env["history_mgr"]

    session_info = {
        "session_id": session_id,
        "base_dir": str(selective_env["base_dir"]),
        "timestamp": 1234567890,
        "items": history_mgr.get_session_moves(session_id),
    }

    app = AutoSorterTUI(settings=AppSettings())
    async with app.run_test() as pilot:
        modal = RollbackConfirmModal(session_info=session_info)
        app.push_screen(modal)
        await pilot.pause()

        dt = modal.query_one("#rollback-items-table", DataTable)
        assert dt.row_count == 3

        count_lbl = modal.query_one("#lbl-selection-count", Label)
        assert "3 / 3" in str(count_lbl.render())

        # Test search filter
        search_inp = modal.query_one("#rollback-search-input", Input)
        search_inp.value = "file1"
        await pilot.pause()
        assert dt.row_count == 1

        # Test Deselect All on filtered item
        modal.query_one("#btn-deselect-all", Button).press()
        await pilot.pause()
        assert "2 / 3" in str(count_lbl.render())

        # Clear search filter
        search_inp.value = ""
        await pilot.pause()
        assert dt.row_count == 3

        # Select All
        modal.query_one("#btn-select-all", Button).press()
        await pilot.pause()
        assert "3 / 3" in str(count_lbl.render())

        # Invert Selection
        modal.query_one("#btn-invert-selection", Button).press()
        await pilot.pause()
        assert "0 / 3" in str(count_lbl.render())


def test_preview_rollback_simulation(selective_env):
    """Test HistoryManager.preview_rollback classifies step statuses without disk side-effects."""
    history_mgr = selective_env["history_mgr"]
    session_id = selective_env["session_id"]
    file1 = selective_env["file1"]
    file2 = selective_env["file2"]
    file3 = selective_env["file3"]
    target1 = selective_env["target1"]
    target2 = selective_env["target2"]

    file2.write_text("file 2 content")
    file3.write_text("different file 3 content")

    target1_mtime = target1.stat().st_mtime
    target2_mtime = target2.stat().st_mtime

    res = history_mgr.preview_rollback(session_id)

    assert target1.stat().st_mtime == target1_mtime
    assert target2.stat().st_mtime == target2_mtime
    assert file2.read_text() == "file 2 content"
    assert file3.read_text() == "different file 3 content"

    assert res["session_id"] == session_id
    assert res["total_operations"] == 3
    assert res["summary"]["clean"] == 1
    assert res["summary"]["identical_exists"] == 1
    assert res["summary"]["collision"] == 1
    assert res["summary"]["missing_target"] == 0
    assert res["has_collisions"] is True

    steps_by_id = {s["step_id"]: s for s in res["steps"]}
    s1 = steps_by_id[selective_env["step1"]]
    s2 = steps_by_id[selective_env["step2"]]
    s3 = steps_by_id[selective_env["step3"]]

    assert s1["status"] == "CLEAN"
    assert s1["collision"] is False
    assert s1["restoration_path"] == str(file1)

    assert s2["status"] == "IDENTICAL_EXISTS"
    assert s2["collision"] is False
    assert s2["restoration_path"] == str(file2)

    assert s3["status"] == "COLLISION"
    assert s3["collision"] is True
    assert s3["restoration_path"] != str(file3)
    assert "file3" in s3["restoration_path"]


def test_preview_rollback_missing_target(selective_env):
    """Test preview_rollback categorizes missing target files as MISSING_TARGET."""
    history_mgr = selective_env["history_mgr"]
    session_id = selective_env["session_id"]
    target1 = selective_env["target1"]

    target1.unlink()

    res = history_mgr.preview_rollback(session_id)

    assert res["summary"]["missing_target"] == 1
    assert res["has_missing"] is True

    steps_by_id = {s["step_id"]: s for s in res["steps"]}
    s1 = steps_by_id[selective_env["step1"]]
    assert s1["status"] == "MISSING_TARGET"


def test_preview_rollback_subset_step_filtering(selective_env):
    """Test preview_rollback filters analyzed steps when selected_step_ids is provided."""
    history_mgr = selective_env["history_mgr"]
    session_id = selective_env["session_id"]
    step1 = selective_env["step1"]

    res = history_mgr.preview_rollback(session_id, selected_step_ids=[step1])

    assert res["total_operations"] == 1
    assert len(res["steps"]) == 1
    assert res["steps"][0]["step_id"] == step1


def test_preview_rollback_snapshot_fallback(selective_env, tmp_path):
    """Test preview_rollback snapshot fallback when step ledger has no recorded moves."""
    history_mgr = selective_env["history_mgr"]
    snap_dir = tmp_path / "snap_workspace"
    snap_dir.mkdir()
    
    # Create files for a snapshot session
    snap_file1 = snap_dir / "snap1.txt"
    snap_file2 = snap_dir / "snap2.txt"
    snap_file1.write_text("snap 1 content")
    snap_file2.write_text("snap 2 content")

    session_id = history_mgr._create_snapshot_internal(str(snap_dir))

    # Remove snap_file2 to test MISSING_TARGET in snapshot fallback
    snap_file2.unlink()

    res = history_mgr.preview_rollback(session_id)

    assert res["session_id"] == session_id
    assert res["total_operations"] == 2
    assert res["summary"]["clean"] == 1
    assert res["summary"]["missing_target"] == 1
    assert res["has_missing"] is True


@pytest.mark.anyio
async def test_two_stage_rollback_confirm_modal_workflow(selective_env):
    """Test two-stage RollbackConfirmModal navigation, dry-run diff review, and back action."""
    session_id = selective_env["session_id"]
    history_mgr = selective_env["history_mgr"]
    file3 = selective_env["file3"]
    file3.write_text("different file 3 content")

    session_info = {
        "session_id": session_id,
        "base_dir": str(selective_env["base_dir"]),
        "timestamp": 1234567890,
        "items": history_mgr.get_session_moves(session_id),
    }

    app = AutoSorterTUI(settings=AppSettings())
    async with app.run_test() as pilot:
        dismiss_result = None

        def handle_dismiss(result):
            nonlocal dismiss_result
            dismiss_result = result

        modal = RollbackConfirmModal(session_info=session_info)
        app.push_screen(modal, handle_dismiss)
        await pilot.pause()

        assert modal.current_stage == 1
        assert modal.query_one("#stage-1-container").styles.display != "none"
        assert modal.query_one("#stage-2-container").styles.display == "none"

        btn_preview = modal.query_one("#btn-preview-dryrun", Button)
        btn_preview.press()
        await pilot.pause()

        assert modal.current_stage == 2
        assert modal.query_one("#stage-1-container").styles.display == "none"
        assert modal.query_one("#stage-2-container").styles.display != "none"

        lbl_collisions = modal.query_one("#lbl-summary-collisions", Label)
        assert "Collisions: 1" in str(lbl_collisions.render())

        diff_table = modal.query_one("#dryrun-diff-table", DataTable)
        assert diff_table.row_count == 3

        btn_back = modal.query_one("#btn-back-to-selection", Button)
        btn_back.press()
        await pilot.pause()

        assert modal.current_stage == 1
        assert modal.query_one("#stage-1-container").styles.display != "none"
        assert modal.query_one("#stage-2-container").styles.display == "none"

        btn_preview.press()
        await pilot.pause()
        assert modal.current_stage == 2

        btn_execute = modal.query_one("#btn-execute-rollback", Button)
        btn_execute.press()
        await pilot.pause()

        assert dismiss_result is not None
        assert dismiss_result["confirmed"] is True
        assert dismiss_result["session_id"] == session_id
