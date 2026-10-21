"""Unit tests for DropZoneModal floating overlay and TUI integration."""

import os

import pytest
from textual.app import App
from textual.events import Paste
from textual.widgets import Button
from textual.worker import WorkerCancelled

from app.ui.tui import AutoSorterTUI, DropPlanConfirmModal, DropZoneModal


class DummySettings:
    """Dummy settings for testing DropZoneModal."""

    def __init__(self, tmp_path):
        self.MAX_QUEUE_CAPACITY = 100
        self.MAX_WORKERS = 2
        self.DEDUP_WINDOW = 0.1
        self.RECONCILIATION_INTERVAL = 60.0
        self.LOG_FILE = str(tmp_path / "test.log")
        self.CONFLICT_POLICY = "rename"
        self.MAX_FOLDERS = 10
        self.STOP_WORDS = set()
        self.AI_CONSENT_GRANTED = False
        self.POLICIES = []
        self.PROTECTED_PATHS = [str(tmp_path / "protected")]
        self.IGNORED_EXTENSIONS = [".tmp"]
        self.SORTING_STRATEGY = "default"

    def load(self):
        pass


class ModalTestApp(App):
    """Test harness App for mounting DropZoneModal."""

    def __init__(self, settings, base_dir):
        super().__init__()
        self.settings = settings
        self.base_dir = base_dir

    def compose(self):
        from textual.widgets import Label

        yield Label("Base Screen")

    def on_mount(self) -> None:
        self.push_screen(DropZoneModal(settings=self.settings, base_dir=self.base_dir))


async def _wait_for(app, pilot, condition) -> None:
    """Let the drop workers run until ``condition()`` holds.

    The workers hand results to the UI with ``call_from_thread``, which
    blocks until the UI has applied them, so each round waits for running
    workers to finish rather than for a fixed time. That keeps a slow
    runner from timing out, while the round limit stops a broken flow
    from hanging the test.
    """
    for _ in range(50):
        await pilot.pause()
        for worker in list(app.workers):
            try:
                await worker.wait()
            except WorkerCancelled:
                # The execute worker dismisses its own screen once the
                # result is set, which cancels it: it has still finished.
                pass
        if condition():
            await pilot.pause()
            return
    pytest.fail("Drop flow never reached the expected state")


@pytest.mark.anyio
async def test_dropzone_modal_a11y_compliance(tmp_path):
    """Verify DropZoneModal satisfies WCAG 2.1 accessibility requirements."""
    settings = DummySettings(tmp_path)

    app = ModalTestApp(settings=settings, base_dir=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        active_modal = app.screen
        assert isinstance(active_modal, DropZoneModal)
        audit = active_modal.audit_a11y_compliance()
        assert audit["compliant"] is True, (
            f"A11y violations found: {audit['violations']}"
        )


@pytest.mark.anyio
async def test_dropzone_modal_paste_and_trigger(tmp_path):
    """Test pasting file path into DropZoneModal and executing sorting worker."""
    settings = DummySettings(tmp_path)
    test_dir = tmp_path / "incoming"
    test_dir.mkdir()
    sample_file = test_dir / "invoice_123.txt"
    sample_file.write_text("Invoice details for customer 123")

    app = ModalTestApp(settings=settings, base_dir=str(test_dir))

    async with app.run_test() as pilot:
        await pilot.pause()
        active_modal = app.screen
        assert isinstance(active_modal, DropZoneModal)

        # Simulate paste event
        paste_evt = Paste(text=str(sample_file))
        active_modal.on_paste(paste_evt)
        await pilot.pause()

        # Trigger triage: it plans first and asks before moving anything.
        active_modal.trigger_drop_triage()
        await _wait_for(
            app, pilot, lambda: isinstance(app.screen, DropPlanConfirmModal)
        )
        assert isinstance(app.screen, DropPlanConfirmModal)
        assert active_modal.processed_result is None
        assert sample_file.exists()

        app.screen.query_one("#btn-confirm-drop-plan", Button).press()

        await _wait_for(app, pilot, lambda: active_modal.processed_result is not None)

        assert active_modal.processed_result is not None
        assert active_modal.processed_result["status"] == "success"
        assert active_modal.processed_result["processed_count"] == 1


@pytest.mark.anyio
async def test_dropzone_cancel_at_review_moves_nothing(tmp_path):
    """Cancelling the plan review leaves every dropped file where it was."""
    settings = DummySettings(tmp_path)
    test_dir = tmp_path / "incoming"
    test_dir.mkdir()
    for i in range(3):
        (test_dir / f"invoice_{i}.txt").write_text(f"Invoice total due {i}")
        (test_dir / f"notes_{i}.txt").write_text(f"Python server deploy {i}")
    before = sorted(p.name for p in test_dir.iterdir())

    app = ModalTestApp(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        dropzone = app.screen
        dropzone.on_paste(
            Paste(text="\n".join(str(p) for p in sorted(test_dir.iterdir())))
        )
        await pilot.pause()
        dropzone.trigger_drop_triage()
        await _wait_for(
            app, pilot, lambda: isinstance(app.screen, DropPlanConfirmModal)
        )
        review = app.screen
        assert isinstance(review, DropPlanConfirmModal)
        assert "Nothing has been moved yet" in review.summary
        audit = review.audit_a11y_compliance()
        assert audit["compliant"] is True, audit["violations"]
        assert app.focused is review.query_one("#btn-cancel-drop-plan", Button)

        await pilot.press("escape")
        await pilot.pause()
        await app.workers.wait_for_complete()

        assert app.screen is dropzone
        assert dropzone.processed_result is None
        assert dropzone._pending_drop == []
        assert sorted(p.name for p in test_dir.iterdir()) == before


@pytest.mark.anyio
async def test_dropzone_modal_protected_path_rejection(tmp_path):
    """Test that DropZoneModal rejects protected paths."""
    settings = DummySettings(tmp_path)
    protected_dir = tmp_path / "protected"
    protected_dir.mkdir()
    prot_file = protected_dir / "secret.txt"
    prot_file.write_text("protected content")

    app = ModalTestApp(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        active_modal = app.screen
        assert isinstance(active_modal, DropZoneModal)

        # Input protected path
        inp = active_modal.query_one("#input-drop-paths")
        inp.value = str(prot_file)
        active_modal.trigger_drop_triage()
        await pilot.pause()

        status_lbl = active_modal.query_one("#dropzone-status")
        assert "Protected path blocked" in str(status_lbl.render())
        assert active_modal.processed_result is None


@pytest.mark.anyio
async def test_autosorter_tui_dropzone_action(tmp_path):
    """Test pressing ctrl+d or calling action_open_dropzone in AutoSorterTUI."""
    settings = DummySettings(tmp_path)
    tui = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with tui.run_test() as pilot:
        await pilot.pause()
        tui.action_open_dropzone()
        await pilot.pause()

        # Check that DropZoneModal is active on top of screen stack
        assert isinstance(tui.screen, DropZoneModal)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(tui.screen, DropZoneModal)


@pytest.mark.anyio
async def test_dropzone_staging_table_population_and_filtering(tmp_path):
    """Test staging table population, columns, and filtering unsupported file types."""
    from textual.widgets import Button, DataTable

    settings = DummySettings(tmp_path)
    test_dir = tmp_path / "staging_test"
    test_dir.mkdir()

    valid_doc = test_dir / "report.pdf"
    valid_doc.write_text("PDF content")

    unsupported_file = test_dir / "image.png"
    unsupported_file.write_text("PNG image data")

    app = ModalTestApp(settings=settings, base_dir=str(test_dir))

    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        active_modal = app.screen
        assert isinstance(active_modal, DropZoneModal)

        # Stage both files
        active_modal.stage_paths(f"{valid_doc}\n{unsupported_file}")
        await pilot.pause()

        table = active_modal.query_one("#dropzone-staging-table", DataTable)
        assert len(active_modal.staged_items) == 2
        assert table.row_count == 2

        # Check column headers
        col_labels = [str(col.label) for col in table.columns.values()]
        assert col_labels == ["Status", "Filename", "Extension", "Size", "Full Path"]

        # Check item statuses
        statuses = [item["status"] for item in active_modal.staged_items]
        assert "Ready" in statuses
        assert "Unsupported" in statuses

        # Press Filter Unsupported button
        active_modal.query_one("#btn-filter-unsupported", Button).press()
        await pilot.pause()

        assert len(active_modal.staged_items) == 1
        assert table.row_count == 1
        assert active_modal.staged_items[0]["status"] == "Ready"
        assert active_modal.staged_items[0]["filename"] == "report.pdf"


@pytest.mark.anyio
async def test_dropzone_remove_and_clear_queue(tmp_path):
    """Test removing selected item, reordering items, and clearing queue."""
    from textual.widgets import Button, DataTable

    settings = DummySettings(tmp_path)
    test_dir = tmp_path / "queue_test"
    test_dir.mkdir()

    file1 = test_dir / "doc1.txt"
    file1.write_text("doc 1")
    file2 = test_dir / "doc2.csv"
    file2.write_text("col1,col2")
    file3 = test_dir / "doc3.docx"
    file3.write_text("doc 3")

    app = ModalTestApp(settings=settings, base_dir=str(test_dir))

    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        active_modal = app.screen
        assert isinstance(active_modal, DropZoneModal)

        active_modal.stage_paths(f"{file1}\n{file2}\n{file3}")
        await pilot.pause()

        table = active_modal.query_one("#dropzone-staging-table", DataTable)
        assert table.row_count == 3

        # Select first row and move down
        table.move_cursor(row=0)
        await pilot.pause()
        await pilot.click("#btn-move-down")
        await pilot.pause()

        assert active_modal.staged_items[0]["filename"] == "doc2.csv"
        assert active_modal.staged_items[1]["filename"] == "doc1.txt"

        # Remove item at cursor (row 1)
        table.move_cursor(row=1)
        await pilot.pause()
        await pilot.click("#btn-remove-item")
        await pilot.pause()

        assert len(active_modal.staged_items) == 2
        filenames = [item["filename"] for item in active_modal.staged_items]
        assert "doc1.txt" not in filenames

        # Clear queue
        active_modal.query_one("#btn-clear-queue", Button).press()
        await pilot.pause()

        assert len(active_modal.staged_items) == 0
        assert table.row_count == 0


@pytest.mark.anyio
async def test_dropzone_empty_queue_blocking(tmp_path):
    """Test that trying to sort an empty queue blocks execution with error message."""
    from textual.widgets import Button

    settings = DummySettings(tmp_path)
    app = ModalTestApp(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        active_modal = app.screen
        assert isinstance(active_modal, DropZoneModal)

        # Click Sort Items on empty queue
        active_modal.query_one("#btn-process", Button).press()
        await pilot.pause()

        status_lbl = active_modal.query_one("#dropzone-status")
        assert "No file or folder paths provided" in str(
            status_lbl.render()
        ) or "No ready files to sort" in str(status_lbl.render())
        assert active_modal.processed_result is None


@pytest.mark.anyio
async def test_dropzone_search_filter_and_metadata_preview(tmp_path):
    """Test interactive search filter input and metadata preview card in DropZoneModal."""
    from textual.widgets import DataTable, Input, Label

    settings = DummySettings(tmp_path)
    test_dir = tmp_path / "search_preview_test"
    test_dir.mkdir()

    file_alpha = test_dir / "alpha_invoice.pdf"
    file_alpha.write_text("Invoice Alpha content")

    file_beta = test_dir / "beta_report.txt"
    file_beta.write_text("Report Beta text")

    file_gamma = test_dir / "gamma_invoice.docx"
    file_gamma.write_text("Docx Gamma content")

    app = ModalTestApp(settings=settings, base_dir=str(test_dir))

    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        active_modal = app.screen
        assert isinstance(active_modal, DropZoneModal)

        # Stage all 3 files
        active_modal.stage_paths(f"{file_alpha}\n{file_beta}\n{file_gamma}")
        await pilot.pause()

        table = active_modal.query_one("#dropzone-staging-table", DataTable)
        filter_inp = active_modal.query_one("#input-queue-filter", Input)
        preview_card = active_modal.query_one("#dropzone-metadata-preview")

        assert table.row_count == 3
        assert preview_card is not None

        # Verify initial metadata preview for active row (row 0: alpha_invoice.pdf)
        table.move_cursor(row=0)
        await pilot.pause()

        lbl_path = active_modal.query_one("#preview-path", Label)
        lbl_size = active_modal.query_one("#preview-size", Label)
        lbl_mtime = active_modal.query_one("#preview-mtime", Label)
        lbl_status = active_modal.query_one("#preview-status", Label)

        assert str(file_alpha) in str(lbl_path.render())
        assert "Ready" in str(lbl_status.render())
        assert "Modified:" in str(lbl_mtime.render())
        assert "N/A" not in str(lbl_mtime.render())

        # Type filter query "invoice"
        filter_inp.value = "invoice"
        await pilot.pause()

        # Should show 2 rows (alpha_invoice and gamma_invoice)
        assert table.row_count == 2

        # Check metadata preview updates on row 1 (gamma_invoice)
        table.move_cursor(row=1)
        await pilot.pause()
        assert str(file_gamma) in str(lbl_path.render())

        # Clear search filter
        filter_inp.value = ""
        await pilot.pause()

        # Should restore all 3 rows
        assert table.row_count == 3


@pytest.mark.anyio
async def test_dropzone_responsive_narrow_layout(tmp_path):
    """Test that narrow viewport under 80 columns applies narrow CSS class for stacked preview."""
    settings = DummySettings(tmp_path)
    app = ModalTestApp(settings=settings, base_dir=str(tmp_path))

    async with app.run_test(size=(75, 40)) as pilot:
        await pilot.pause()
        active_modal = app.screen
        assert isinstance(active_modal, DropZoneModal)

        # Should have .narrow class applied
        assert active_modal.has_class("narrow")


@pytest.mark.anyio
async def test_dropzone_folder_drop_stages_and_moves_its_files(tmp_path):
    """Dropping a folder stages every file in it as Ready and sorts them together."""
    settings = DummySettings(tmp_path)
    test_dir = tmp_path / "incoming"
    test_dir.mkdir()
    for i in range(3):
        (test_dir / f"invoice_{i}.txt").write_text(f"Invoice total due {i}")
        (test_dir / f"notes_{i}.txt").write_text(f"Python server deploy {i}")

    app = ModalTestApp(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        dropzone = app.screen
        dropzone.on_paste(Paste(text=str(test_dir)))
        await pilot.pause()

        statuses = [item["status"] for item in dropzone.staged_items]
        assert statuses == ["Ready"] * 6
        assert all(os.path.isabs(item["path"]) for item in dropzone.staged_items)

        dropzone.trigger_drop_triage()
        await _wait_for(
            app, pilot, lambda: isinstance(app.screen, DropPlanConfirmModal)
        )
        review = app.screen
        assert isinstance(review, DropPlanConfirmModal)
        assert "Move 6 file(s)" in review.summary
        # One planning session for the whole folder, not one per file.
        assert len(dropzone._pending_drop) == 1

        review.query_one("#btn-confirm-drop-plan", Button).press()
        await _wait_for(app, pilot, lambda: dropzone.processed_result is not None)

        assert dropzone.processed_result["status"] == "success"
        assert dropzone.processed_result["processed_count"] == 6
        loose = [p for p in test_dir.iterdir() if p.is_file()]
        assert loose == []


def test_dropzone_worker_batch_error_isolation(mocker, tmp_path):
    """Verify mid-batch move failures let remaining items execute and close all sessions."""
    settings = DummySettings(tmp_path)
    dropzone = DropZoneModal(settings=settings, base_dir=str(tmp_path))

    session1 = mocker.MagicMock()
    session2 = mocker.MagicMock()
    session3 = mocker.MagicMock()

    item1 = {
        "path": "/path/item1.txt",
        "session": session1,
        "plan": {"moves": []},
        "file_count": 1,
    }
    item2 = {
        "path": "/path/item2.txt",
        "session": session2,
        "plan": {"moves": []},
        "file_count": 2,
    }
    item3 = {
        "path": "/path/item3.txt",
        "session": session3,
        "plan": {"moves": []},
        "file_count": 1,
    }

    dropzone._pending_drop = [item1, item2, item3]

    outcome1 = mocker.MagicMock()
    outcome1.summary = {"moved": 1}
    outcome1.describe.return_value = "1 moved."

    outcome3 = mocker.MagicMock()
    outcome3.summary = {"moved": 1}
    outcome3.describe.return_value = "1 moved."

    def mock_pipeline_init(session):
        pipeline = mocker.MagicMock()
        if session is session2:
            pipeline.execute.side_effect = RuntimeError("Permission denied during move")
        elif session is session1:
            pipeline.execute.return_value = outcome1
        elif session is session3:
            pipeline.execute.return_value = outcome3
        return pipeline

    mocker.patch("app.core.pipeline.SortPipeline", side_effect=mock_pipeline_init)
    mocker.patch.object(dropzone, "dismiss")
    mocker.patch.object(dropzone, "update_status_msg")

    dropzone.run_drop_execute_worker.__wrapped__(dropzone)

    session1.close.assert_called_once()
    session2.close.assert_called_once()
    session3.close.assert_called_once()

    res = dropzone.processed_result
    assert res is not None
    assert res["status"] == "partial"
    assert res["processed_count"] == 2
    assert res["succeeded_count"] == 2
    assert res["failed_count"] == 1
    assert res["total_count"] == 3
    assert len(res["items"]) == 2
    assert len(res["failed_items"]) == 1
    assert res["failed_items"][0]["path"] == "/path/item2.txt"
    assert "Permission denied during move" in res["failed_items"][0]["error"]


def test_dropzone_worker_total_failure_and_cleanup(mocker, tmp_path):
    """Verify total batch move failure closes all sessions and reports failure status."""
    settings = DummySettings(tmp_path)
    dropzone = DropZoneModal(settings=settings, base_dir=str(tmp_path))

    session1 = mocker.MagicMock()
    session2 = mocker.MagicMock()

    item1 = {
        "path": "/path/item1.txt",
        "session": session1,
        "plan": {"moves": []},
        "file_count": 1,
    }
    item2 = {
        "path": "/path/item2.txt",
        "session": session2,
        "plan": {"moves": []},
        "file_count": 1,
    }

    dropzone._pending_drop = [item1, item2]

    def mock_pipeline_init(session):
        pipeline = mocker.MagicMock()
        pipeline.execute.side_effect = OSError("Disk full")
        return pipeline

    mocker.patch("app.core.pipeline.SortPipeline", side_effect=mock_pipeline_init)
    mocker.patch.object(dropzone, "dismiss")
    mocker.patch.object(dropzone, "update_status_msg")

    dropzone.run_drop_execute_worker.__wrapped__(dropzone)

    session1.close.assert_called_once()
    session2.close.assert_called_once()

    res = dropzone.processed_result
    assert res is not None
    assert res["status"] == "failed"
    assert res["processed_count"] == 0
    assert res["succeeded_count"] == 0
    assert res["failed_count"] == 2
    assert res["total_count"] == 2
    assert len(res["items"]) == 0
    assert len(res["failed_items"]) == 2
