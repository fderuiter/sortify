"""Unit and integration tests for batch quarantine operations across core service, CLI, and TUI modal."""

import argparse
import asyncio
import json
import os
import tempfile

import pytest

from app.cli.quarantine_cli import handle_quarantine_command
from app.config import AppSettings
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.quarantine_interceptor import QuarantineInterceptorService
from app.ui.modals.quarantine import QuarantineVaultModal
from app.ui.tui import AutoSorterTUI


@pytest.fixture
def test_db_and_dir():
    """Create a temporary database and base directory for quarantine batch testing."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "test_quarantine.db")
        db = Database(db_path, DBWorker())
        yield db, tmp_dir


def stage_dummy_file(
    service: QuarantineInterceptorService, base_dir: str, filename: str
) -> str:
    """Helper to create and stage a dummy file for quarantine testing."""
    src_path = os.path.join(base_dir, filename)
    with open(src_path, "w", encoding="utf-8") as f:
        f.write(f"Dummy content for {filename}\n")
    info = service.stage_incoming_file(source_path=src_path, base_dir=base_dir)
    return info["job_id"]


def test_process_quarantine_batch(test_db_and_dir):
    """Test process_quarantine_batch with multiple items, duplicates, and partial failure isolation."""
    db, tmp_dir = test_db_and_dir
    service = QuarantineInterceptorService(db=db)

    job1 = stage_dummy_file(service, tmp_dir, "file1.txt")
    job2 = stage_dummy_file(service, tmp_dir, "file2.txt")
    invalid_job = "qjob_non_existent_999"

    # Batch with valid jobs, duplicates, and invalid job
    job_ids = [job1, job2, job1, invalid_job]
    results = service.process_quarantine_batch(job_ids)

    # Deduped result count: job1, job2, invalid_job -> 3 unique items
    assert len(results) == 3

    statuses = {r["job_id"]: r.get("status") for r in results}
    assert statuses[job1] in (
        "QUARANTINED",
        "CLEARED",
        "RELEASED",
        "MANUAL_REVIEW_REQUIRED",
    )
    assert statuses[job2] in (
        "QUARANTINED",
        "CLEARED",
        "RELEASED",
        "MANUAL_REVIEW_REQUIRED",
    )
    assert statuses[invalid_job] in ("FAILED", "DEAD_LETTER_QUEUE")


def test_release_quarantine_batch(test_db_and_dir):
    """Test release_quarantine_batch releasing multiple staged files to target directory."""
    db, tmp_dir = test_db_and_dir
    service = QuarantineInterceptorService(db=db)

    job1 = stage_dummy_file(service, tmp_dir, "doc1.pdf")
    job2 = stage_dummy_file(service, tmp_dir, "doc2.pdf")
    out_dir = os.path.join(tmp_dir, "released_output")

    results = service.release_quarantine_batch([job1, job2], dest_dir=out_dir)

    assert len(results) == 2
    for res in results:
        assert res["status"] == "RELEASED"
        dest = res["destination"]
        assert os.path.exists(dest)

    rec1 = db.get_quarantine_record(job1)
    rec2 = db.get_quarantine_record(job2)
    assert rec1["status"] == "RELEASED"
    assert rec2["status"] == "RELEASED"


def test_dismiss_quarantine_batch(test_db_and_dir):
    """Test dismiss_quarantine_batch marking items DISMISSED while leaving staged files."""
    db, tmp_dir = test_db_and_dir
    service = QuarantineInterceptorService(db=db)

    job1 = stage_dummy_file(service, tmp_dir, "dismiss1.txt")
    job2 = stage_dummy_file(service, tmp_dir, "dismiss2.txt")

    rec1_before = db.get_quarantine_record(job1)
    staged_path1 = rec1_before["staged_filepath"]

    results = service.dismiss_quarantine_batch([job1, job2])

    assert len(results) == 2
    for res in results:
        assert res["status"] == "DISMISSED"

    rec1_after = db.get_quarantine_record(job1)
    rec2_after = db.get_quarantine_record(job2)
    assert rec1_after["status"] == "DISMISSED"
    assert rec2_after["status"] == "DISMISSED"
    # Staged file remains on disk
    assert os.path.exists(staged_path1)

    # Re-dismissing should indicate already dismissed
    re_dismiss = service.dismiss_quarantine_batch([job1])
    assert re_dismiss[0]["status"] == "DISMISSED"
    assert "already dismissed" in re_dismiss[0]["details"].lower()


def test_cli_quarantine_batch_commands(test_db_and_dir, capsys):
    """Test CLI process, release, and dismiss subcommands with batch positional/option inputs."""
    db, tmp_dir = test_db_and_dir
    service = QuarantineInterceptorService(db=db)

    job1 = stage_dummy_file(service, tmp_dir, "cli1.txt")
    job2 = stage_dummy_file(service, tmp_dir, "cli2.txt")

    # CLI process batch
    args_proc = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="process",
        job_ids_pos=[job1, job2],
        job_ids=None,
        job_id=None,
        timeout=30.0,
        db_path=db.db_path,
        quiet=True,
        json=True,
    )

    with pytest.raises(SystemExit) as exc_info:
        handle_quarantine_command(args_proc, None)
    assert exc_info.value.code == 0

    captured = capsys.readouterr()
    output_data = json.loads(captured.out)
    assert output_data["status"] == "success"
    assert output_data["count"] == 2

    # CLI release batch
    out_dir = os.path.join(tmp_dir, "cli_release_out")
    args_rel = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="release",
        job_ids_pos=None,
        job_ids=f"{job1},{job2}",
        dest_dir=out_dir,
        db_path=db.db_path,
        quiet=True,
        json=True,
    )

    with pytest.raises(SystemExit) as exc_info:
        handle_quarantine_command(args_rel, None)
    assert exc_info.value.code == 0

    captured = capsys.readouterr()
    output_data = json.loads(captured.out)
    assert output_data["status"] == "success"
    assert output_data["count"] == 2

    # CLI dismiss batch
    job3 = stage_dummy_file(service, tmp_dir, "cli3.txt")
    args_dis = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="dismiss",
        job_ids_pos=[job3],
        job_ids=None,
        db_path=db.db_path,
        quiet=True,
        json=True,
    )

    with pytest.raises(SystemExit) as exc_info:
        handle_quarantine_command(args_dis, None)
    assert exc_info.value.code == 0

    rec3 = db.get_quarantine_record(job3)
    assert rec3["status"] == "DISMISSED"


def test_quarantine_vault_modal_multi_selection(test_db_and_dir):
    """Test QuarantineVaultModal multi-selection, select-all, and selection state tracking."""
    db, tmp_dir = test_db_and_dir
    service = QuarantineInterceptorService(db=db)

    job1 = stage_dummy_file(service, tmp_dir, "ui1.txt")
    job2 = stage_dummy_file(service, tmp_dir, "ui2.txt")

    async def _test():
        modal = QuarantineVaultModal(db=db, base_dir=tmp_dir)
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)

        async with app.run_test() as pilot:
            await app.push_screen(modal)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()

            table = modal.query_one("#quarantine-table")
            assert table.row_count == 2

            # Select All check
            select_all_chk = modal.query_one("#chk-select-all")
            select_all_chk.value = True
            await pilot.pause()

            assert len(modal.selected_job_ids) == 2
            assert job1 in modal.selected_job_ids
            assert job2 in modal.selected_job_ids

            # Deselect All check
            select_all_chk.value = False
            await pilot.pause()

            assert len(modal.selected_job_ids) == 0

            # Toggle single row selection
            modal.selected_job_ids.add(job1)
            modal._update_selection_count_label()
            await pilot.pause()

            count_lbl = modal.query_one("#lbl-selection-count")
            assert "Selected: 1/2" in str(count_lbl.render())

    asyncio.run(_test())
