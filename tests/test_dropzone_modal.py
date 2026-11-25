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
        self.KEYWORD_RULES: dict[str, str] = {}
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
    """Test staging table population, columns, universal formats, and filtering unsupported types."""
    from textual.widgets import Button, DataTable

    settings = DummySettings(tmp_path)
    test_dir = tmp_path / "staging_test"
    test_dir.mkdir()

    valid_doc = test_dir / "report.pdf"
    valid_doc.write_text("PDF content")

    image_file = test_dir / "image.png"
    image_file.write_text("PNG image data")

    media_file = test_dir / "clip.mp4"
    media_file.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    archive_file = test_dir / "bundle.zip"
    archive_file.write_bytes(b"PK\x03\x04")

    ignored_file = test_dir / "scratch.tmp"
    ignored_file.write_text("temp scratch")

    unsupported_file = test_dir / "mystery.xyz123"
    unsupported_file.write_text("unknown format")

    app = ModalTestApp(settings=settings, base_dir=str(test_dir))

    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        active_modal = app.screen
        assert isinstance(active_modal, DropZoneModal)

        # Stage supported (documents, images, media, archives), ignored, and unknown files
        active_modal.stage_paths(
            "\n".join(
                str(p)
                for p in (
                    valid_doc,
                    image_file,
                    media_file,
                    archive_file,
                    ignored_file,
                    unsupported_file,
                )
            )
        )
        await pilot.pause()

        table = active_modal.query_one("#dropzone-staging-table", DataTable)
        assert len(active_modal.staged_items) == 6
        assert table.row_count == 6

        # Check column headers
        col_labels = [str(col.label) for col in table.columns.values()]
        assert col_labels == ["Status", "Filename", "Extension", "Size", "Full Path"]

        # Check item statuses
        status_by_name = {
            item["filename"]: item["status"] for item in active_modal.staged_items
        }
        assert status_by_name == {
            "report.pdf": "Ready",
            "image.png": "Ready",
            "clip.mp4": "Ready",
            "bundle.zip": "Ready",
            "scratch.tmp": "Unsupported",
            "mystery.xyz123": "Ready",
        }

        # Press Filter Unsupported button (removes ignored extensions like .tmp, keeps all non-ignored files)
        active_modal.query_one("#btn-filter-unsupported", Button).press()
        await pilot.pause()

        assert len(active_modal.staged_items) == 5
        assert table.row_count == 5
        assert all(item["status"] == "Ready" for item in active_modal.staged_items)
        assert [item["filename"] for item in active_modal.staged_items] == [
            "report.pdf",
            "image.png",
            "clip.mp4",
            "bundle.zip",
            "mystery.xyz123",
        ]


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


@pytest.mark.anyio
async def test_dropzone_multipath_quotes_uris_and_unquoted_spaces(tmp_path):
    """stage_paths handles unquoted existing spaces, quoted multi-paths, file:// URIs, and rule matches."""
    settings = DummySettings(tmp_path)
    settings.KEYWORD_RULES = {"ledger": "Finance"}

    dir_spaces = tmp_path / "my folder"
    dir_spaces.mkdir()
    spaced_doc = dir_spaces / "annual report.pdf"
    spaced_doc.write_text("Annual report content")

    dir_one = tmp_path / "dir one"
    dir_one.mkdir()
    deck_file = dir_one / "pitch deck.pptx"
    deck_file.write_bytes(b"PK\x03\x04")

    dir_two = tmp_path / "dir two"
    dir_two.mkdir()
    audio_file = dir_two / "voice note.mp3"
    audio_file.write_bytes(b"ID3")

    uri_dir = tmp_path / "uri folder"
    uri_dir.mkdir()
    uri_zip = uri_dir / "archive bundle.zip"
    uri_zip.write_bytes(b"PK\x03\x04")
    uri_str = uri_zip.resolve().as_uri()

    rule_file = tmp_path / "q4_ledger.customext"
    rule_file.write_text("custom ledger")

    app = ModalTestApp(settings=settings, base_dir=str(tmp_path))
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, DropZoneModal)

        payload = f"{spaced_doc}\n\"{deck_file}\" '{audio_file}'\n{uri_str} {rule_file}"
        modal.stage_paths(payload)
        await pilot.pause()

        assert [item["filename"] for item in modal.staged_items] == [
            "annual report.pdf",
            "pitch deck.pptx",
            "voice note.mp3",
            "archive bundle.zip",
            "q4_ledger.customext",
        ]
        assert all(item["status"] == "Ready" for item in modal.staged_items)


@pytest.mark.anyio
async def test_dropzone_nested_folder_routes_to_selected_destination(
    tmp_path, monkeypatch
):
    """Dropping a folder with nested subfolders plans in one session and routes into destination."""
    from textual.widgets import Input

    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app_dir"))
    settings = DummySettings(tmp_path)
    incoming = tmp_path / "incoming_nested"
    (incoming / "sub_a").mkdir(parents=True)
    (incoming / "sub_b").mkdir(parents=True)
    (incoming / "sub_a" / "invoice_1.txt").write_text("Invoice total due 100")
    (incoming / "sub_b" / "bundle_2.zip").write_bytes(b"PK\x03\x04")

    dest_dir = tmp_path / "chosen_dest"
    dest_dir.mkdir()

    app = ModalTestApp(settings=settings, base_dir=str(tmp_path))
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        dropzone = app.screen
        assert isinstance(dropzone, DropZoneModal)

        dest_inp = dropzone.query_one("#input-dest-dir", Input)
        assert dest_inp.value == ""

        # Focused paste on #input-dest-dir must NOT stage the destination path
        dest_inp.focus()
        await pilot.pause()
        dropzone.on_paste(Paste(text=str(dest_dir)))
        await pilot.pause()
        assert dropzone.staged_items == []

        dest_inp.value = str(dest_dir)
        dropzone.query_one("#input-drop-paths", Input).focus()
        await pilot.pause()
        dropzone.on_paste(Paste(text=str(incoming)))
        await pilot.pause()

        assert len(dropzone.staged_items) == 2
        assert all(
            item["group_root"] == str(incoming) for item in dropzone.staged_items
        )

        dropzone.trigger_drop_triage()
        await _wait_for(
            app, pilot, lambda: isinstance(app.screen, DropPlanConfirmModal)
        )
        assert len(dropzone._pending_drop) == 1
        session_obj = dropzone._pending_drop[0]["session"]
        assert session_obj.source_dirs == [str(incoming)]
        assert session_obj.base_dir == str(dest_dir.resolve())

        app.screen.query_one("#btn-confirm-drop-plan", Button).press()
        await _wait_for(app, pilot, lambda: dropzone.processed_result is not None)

        assert dropzone.processed_result["status"] == "success"
        assert dropzone.processed_result["processed_count"] == 2
        assert dropzone.processed_result["destination_dir"] == str(dest_dir.resolve())
        assert dropzone.processed_result["session_id"]
        moved_names = {p.name for p in dest_dir.rglob("*") if p.is_file()}
        assert moved_names == {"invoice_1.txt", "bundle_2.zip"}


@pytest.mark.anyio
async def test_dropzone_browse_button_and_native_dialog_error_fallback(
    tmp_path, mocker
):
    """Browse populates #input-dest-dir on selection and keeps manual input active on NativeDialogError."""
    from textual.widgets import Input, Label

    from app.core.native_dialog import NativeDialogError

    settings = DummySettings(tmp_path)
    chosen_dir = tmp_path / "picked_dest"
    chosen_dir.mkdir()

    app = ModalTestApp(settings=settings, base_dir=str(tmp_path))
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, DropZoneModal)

        mocker.patch(
            "app.core.native_dialog.select_native_directory",
            return_value=str(chosen_dir),
        )
        modal.query_one("#btn-browse-dest", Button).press()
        await _wait_for(
            app,
            pilot,
            lambda: (
                modal.query_one("#input-dest-dir", Input).value
                == str(chosen_dir.resolve())
            ),
        )

        mocker.patch(
            "app.core.native_dialog.select_native_directory",
            side_effect=NativeDialogError("No GUI available on headless Linux"),
        )
        modal.query_one("#btn-browse-dest", Button).press()
        await _wait_for(
            app,
            pilot,
            lambda: (
                "unavailable"
                in str(modal.query_one("#dropzone-status", Label).render()).lower()
            ),
        )
        dest_inp = modal.query_one("#input-dest-dir", Input)
        assert app.focused is dest_inp
        assert dest_inp.disabled is False


@pytest.mark.anyio
async def test_dropzone_blocks_protected_destination_path(tmp_path):
    """Setting destination inside PROTECTED_PATHS blocks triage and reveals Help button."""
    from textual.widgets import Input, Label

    settings = DummySettings(tmp_path)
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    doc = src_dir / "invoice.txt"
    doc.write_text("Invoice 42")

    protected_dest = tmp_path / "protected" / "out"

    app = ModalTestApp(settings=settings, base_dir=str(src_dir))
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, DropZoneModal)

        modal.stage_paths(str(doc))
        modal.query_one("#input-dest-dir", Input).value = str(protected_dest)
        await pilot.pause()

        modal.trigger_drop_triage()
        await pilot.pause()

        status_text = str(modal.query_one("#dropzone-status", Label).render())
        assert "Protected destination blocked" in status_text
        assert not modal.query_one("#btn-help", Button).has_class("hidden")
        assert doc.exists()


@pytest.mark.anyio
async def test_tui_open_dropzone_stages_initial_paths_on_mount(tmp_path, monkeypatch):
    """AutoSorterTUI(open_dropzone=True, initial_drop_paths=[...]) opens DropZoneModal with staged paths."""
    from textual.widgets import Input

    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app_dir"))
    settings = DummySettings(tmp_path)
    settings.AI_CONSENT_GRANTED = True
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    sample = src_dir / "photo.png"
    sample.write_text("image bytes")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    tui = AutoSorterTUI(
        settings=settings,
        base_dir=str(dest_dir),
        skip_wizard=True,
        open_dropzone=True,
        initial_drop_paths=[str(sample)],
    )
    async with tui.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        assert isinstance(tui.screen, DropZoneModal)
        modal = tui.screen
        assert modal.query_one("#input-dest-dir", Input).value == ""
        assert len(modal.staged_items) == 1
        assert modal.staged_items[0]["filename"] == "photo.png"
        assert modal.staged_items[0]["status"] == "Ready"

    tui_with_dest = AutoSorterTUI(
        settings=settings,
        base_dir=str(dest_dir),
        skip_wizard=True,
        open_dropzone=True,
        initial_drop_paths=[str(sample)],
        initial_dest_dir=str(dest_dir),
    )
    async with tui_with_dest.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        assert isinstance(tui_with_dest.screen, DropZoneModal)
        modal_with_dest = tui_with_dest.screen
        assert modal_with_dest.query_one("#input-dest-dir", Input).value == str(
            dest_dir.resolve()
        )


@pytest.mark.anyio
async def test_dropzone_blocks_app_bundle_and_quarantine_and_routes_sketch_fig_to_misc(
    tmp_path, monkeypatch
):
    """Dropping Calculator.app or _Compliance_Quarantine marks BLOCKED with 0 internal files while .sketch/.fig sort to Miscellaneous."""
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app_dir"))
    settings = DummySettings(tmp_path)
    work = tmp_path / "work"
    work.mkdir()

    calc_app = work / "Calculator.app"
    (calc_app / "Contents" / "MacOS").mkdir(parents=True)
    app_exe = calc_app / "Contents" / "MacOS" / "Calculator"
    app_exe.write_text("binary")

    quarantine = work / "_Compliance_Quarantine"
    quarantine.mkdir()
    quarantine_doc = quarantine / "quarantined.pdf"
    quarantine_doc.write_text("sensitive")

    sketch_file = work / "ui_kit.sketch"
    sketch_file.write_bytes(b"\x89SKETCH")
    fig_file = work / "flow.fig"
    fig_file.write_bytes(b"\x89FIGMA")

    app = ModalTestApp(settings=settings, base_dir=str(work))
    async with app.run_test(size=(110, 50)) as pilot:
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, DropZoneModal)

        modal.stage_paths(
            "\n".join(str(p) for p in (calc_app, quarantine, sketch_file, fig_file))
        )
        await pilot.pause()

        by_name = {it["filename"]: it["status"] for it in modal.staged_items}
        assert by_name == {
            "Calculator.app": "BLOCKED",
            "_Compliance_Quarantine": "BLOCKED",
            "ui_kit.sketch": "Ready",
            "flow.fig": "Ready",
        }
        # Zero internal files from Calculator.app or _Compliance_Quarantine were staged
        assert len(modal.staged_items) == 4

        modal.trigger_drop_triage()
        await _wait_for(
            app, pilot, lambda: isinstance(app.screen, DropPlanConfirmModal)
        )
        app.screen.query_one("#btn-confirm-drop-plan", Button).press()
        await _wait_for(app, pilot, lambda: modal.processed_result is not None)

        assert modal.processed_result["status"] == "success"
        assert (work / "Miscellaneous" / "ui_kit.sketch").exists()
        assert (work / "Miscellaneous" / "flow.fig").exists()
        assert app_exe.exists()
        assert quarantine_doc.exists()


@pytest.mark.anyio
async def test_dropzone_queue_priority_preview_overrides_workspace_sync_and_ctrl_z_undo(
    tmp_path, monkeypatch
):
    """Moving a colliding item up wins clean name; confirm modal renders warnings/overrides; destination_dir syncs empty base_dir; Ctrl+Z undoes multi-folder drop."""
    from textual.widgets import DataTable, Input, Static

    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app_dir"))
    settings = DummySettings(tmp_path)
    settings.AI_CONSENT_GRANTED = True

    folder_1 = tmp_path / "folder_1"
    folder_2 = tmp_path / "folder_2"
    dest_dir = tmp_path / "shared_dest"
    folder_1.mkdir()
    folder_2.mkdir()
    dest_dir.mkdir()

    col_1 = folder_1 / "mockup.sketch"
    col_2 = folder_2 / "mockup.sketch"
    override_doc = folder_1 / "brand.fig"
    exclude_doc = folder_2 / "draft.fig"
    col_1.write_text("first_staged")
    col_2.write_text("moved_up_winner")
    override_doc.write_text("custom_folder")
    exclude_doc.write_text("stay_put")

    # Start AutoSorterTUI with empty base_dir to verify AC 9 workspace sync
    tui = AutoSorterTUI(
        settings=settings,
        base_dir="",
        skip_wizard=True,
    )
    scan_calls: list[str] = []
    orig_scan = tui.action_scan_directory

    def spy_scan():
        scan_calls.append(tui.base_dir)
        return orig_scan()

    monkeypatch.setattr(tui, "action_scan_directory", spy_scan)

    async with tui.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        tui.action_open_dropzone()
        await _wait_for(tui, pilot, lambda: isinstance(tui.screen, DropZoneModal))
        dropzone = tui.screen
        assert isinstance(dropzone, DropZoneModal)
        assert dropzone.query_one("#input-dest-dir", Input).value == ""

        dropzone.stage_paths(
            "\n".join(str(p) for p in (col_1, col_2, override_doc, exclude_doc))
        )
        await pilot.pause()

        # Move col_2 (row index 1) up above col_1 (row index 0)
        staging_table = dropzone.query_one("#dropzone-staging-table", DataTable)
        staging_table.move_cursor(row=1)
        await pilot.pause()
        dropzone.move_selected_item_up()
        await pilot.pause()
        assert dropzone.staged_items[0]["path"] == str(col_2.resolve())
        assert dropzone.staged_items[1]["path"] == str(col_1.resolve())

        dropzone.query_one("#input-dest-dir", Input).value = str(dest_dir)
        await pilot.pause()
        dropzone.trigger_drop_triage()
        await _wait_for(
            tui, pilot, lambda: isinstance(tui.screen, DropPlanConfirmModal)
        )

        confirm_modal = tui.screen
        assert isinstance(confirm_modal, DropPlanConfirmModal)
        warnings_banner = confirm_modal.query_one("#drop-plan-warnings", Static)
        assert not warnings_banner.has_class("hidden")
        assert "mockup.sketch" in str(warnings_banner.render())

        # Override brand.fig (row 2) folder to CustomBrand and exclude draft.fig (row 3)
        plan_table = confirm_modal.query_one("#drop-plan-items-table", DataTable)
        plan_table.move_cursor(row=2)
        await pilot.pause()
        confirm_modal.query_one(
            "#input-plan-folder-override", Input
        ).value = "CustomBrand"
        await pilot.pause()
        confirm_modal.apply_folder_override()
        await pilot.pause()

        plan_table.move_cursor(row=3)
        await pilot.pause()
        confirm_modal.toggle_selected_exclusion()
        await pilot.pause()

        confirm_modal.query_one("#btn-confirm-drop-plan", Button).press()
        await _wait_for(
            tui,
            pilot,
            lambda: (
                not isinstance(tui.screen, DropZoneModal)
                and not isinstance(tui.screen, DropPlanConfirmModal)
            ),
        )
        await pilot.pause()

        # AC 6: Higher-ranked col_2 won clean mockup.sketch
        assert (
            dest_dir / "Miscellaneous" / "mockup.sketch"
        ).read_text() == "moved_up_winner"
        assert (
            dest_dir / "Miscellaneous" / "mockup_1.sketch"
        ).read_text() == "first_staged"
        # AC 7: Folder override and exclusion applied
        assert (dest_dir / "CustomBrand" / "brand.fig").read_text() == "custom_folder"
        assert exclude_doc.exists()
        # AC 9: Empty base_dir updated to destination_dir and action_scan_directory triggered
        assert tui.base_dir == str(dest_dir.resolve())
        assert str(dest_dir.resolve()) in scan_calls

        # Wait for background scan worker if active before testing Ctrl+Z undo
        await _wait_for(tui, pilot, lambda: not any(w.is_running for w in tui.workers))

        # AC 2 & AC 8: Multi-folder in-place drop (blank #input-dest-dir) + single Ctrl+Z undo
        in_a = tmp_path / "inplace_a"
        in_b = tmp_path / "inplace_b"
        in_a.mkdir()
        in_b.mkdir()
        f_a = in_a / "one.sketch"
        f_b = in_b / "two.fig"
        f_a.write_text("one")
        f_b.write_text("two")

        tui.action_open_dropzone()
        await _wait_for(tui, pilot, lambda: isinstance(tui.screen, DropZoneModal))
        dz2 = tui.screen
        assert isinstance(dz2, DropZoneModal)
        assert dz2.query_one("#input-dest-dir", Input).value == ""
        dz2.stage_paths(f"{f_a}\n{f_b}")
        await pilot.pause()
        dz2.trigger_drop_triage()
        await _wait_for(
            tui, pilot, lambda: isinstance(tui.screen, DropPlanConfirmModal)
        )
        tui.screen.query_one("#btn-confirm-drop-plan", Button).press()
        await _wait_for(
            tui,
            pilot,
            lambda: (
                not isinstance(tui.screen, DropZoneModal)
                and not isinstance(tui.screen, DropPlanConfirmModal)
            ),
        )
        await pilot.pause()

        assert (in_a / "Miscellaneous" / "one.sketch").exists()
        assert (in_b / "Miscellaneous" / "two.fig").exists()

        # Press Ctrl+Z to undo the multi-folder drop in one step
        from app.ui.tui import RollbackConfirmModal

        await pilot.press("ctrl+z")
        await _wait_for(
            tui, pilot, lambda: isinstance(tui.screen, RollbackConfirmModal)
        )
        tui.screen.query_one("#btn-confirm-rollback", Button).press()
        await _wait_for(tui, pilot, lambda: f_a.exists() and f_b.exists())
        assert f_a.read_text() == "one"
        assert f_b.read_text() == "two"


@pytest.mark.anyio
async def test_dropzone_two_folder_unified_batch_workspace_sync_and_single_ctrl_z_undo(
    tmp_path, monkeypatch
):
    """Two-folder DropZoneModal drop creates one unified plan & session, clusters .md/.html across folders, syncs base_dir & source_dirs on empty workspace, and undoes all files on one Ctrl+Z."""
    from textual.widgets import Input

    from app.core.pipeline import SortPipeline
    from app.core.services import acquire_services
    from app.ui.tui import RollbackConfirmModal

    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app_dz_unified"))
    settings = DummySettings(tmp_path)
    settings.AI_CONSENT_GRANTED = False

    dir_alpha = tmp_path / "alpha_inbox"
    dir_beta = tmp_path / "beta_inbox"
    dest_dir = tmp_path / "populated_workspace"
    dir_alpha.mkdir()
    dir_beta.mkdir()
    dest_dir.mkdir()

    neuro_md = dir_alpha / "synaptic_plasticity_cortex.md"
    neuro_html = dir_beta / "synaptic_neurotransmitter_axon.html"
    solar_md = dir_alpha / "photovoltaic_inverter_grid.md"
    solar_txt = dir_beta / "photovoltaic_solar_irradiance.txt"

    neuro_md.write_text(
        "# Neuroscience\nSynaptic plasticity cortex neurotransmitter axon dendrite neuron action potential.\n",
        encoding="utf-8",
    )
    neuro_html.write_text(
        "<html><body><h1>Axon Signaling</h1><p>Synaptic plasticity cortex neurotransmitter axon dendrite neuron action potential.</p></body></html>",
        encoding="utf-8",
    )
    solar_md.write_text(
        "# Solar Grid\nPhotovoltaic solar irradiance inverter grid kilowatt battery array panel.\n",
        encoding="utf-8",
    )
    solar_txt.write_text(
        "Photovoltaic solar irradiance inverter grid kilowatt battery array panel.\n",
        encoding="utf-8",
    )

    plan_calls = 0
    exec_calls = 0
    orig_plan = SortPipeline.plan
    orig_execute = SortPipeline.execute

    def spy_plan(self, *args, **kwargs):
        nonlocal plan_calls
        plan_calls += 1
        return orig_plan(self, *args, **kwargs)

    def spy_execute(self, *args, **kwargs):
        nonlocal exec_calls
        exec_calls += 1
        return orig_execute(self, *args, **kwargs)

    monkeypatch.setattr(SortPipeline, "plan", spy_plan)
    monkeypatch.setattr(SortPipeline, "execute", spy_execute)

    tui = AutoSorterTUI(
        settings=settings,
        base_dir="",
        skip_wizard=True,
    )
    # Simulate stale/pre-populated source_dirs while base_dir is still empty
    tui.source_dirs = [str(tmp_path / "stale_unscanned")]

    scan_snapshots: list[tuple[str, list[str]]] = []
    orig_scan = tui.action_scan_directory

    def spy_scan():
        scan_snapshots.append((tui.base_dir, list(tui.source_dirs)))
        return orig_scan()

    monkeypatch.setattr(tui, "action_scan_directory", spy_scan)

    async with tui.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        tui.action_open_dropzone()
        await _wait_for(tui, pilot, lambda: isinstance(tui.screen, DropZoneModal))
        dropzone = tui.screen
        assert isinstance(dropzone, DropZoneModal)

        dropzone.stage_paths(f"{dir_alpha}\n{dir_beta}")
        await pilot.pause()
        dropzone.query_one("#input-dest-dir", Input).value = str(dest_dir)
        await pilot.pause()

        dropzone.trigger_drop_triage()
        await _wait_for(
            tui, pilot, lambda: isinstance(tui.screen, DropPlanConfirmModal)
        )

        # Single planning pass, single pending drop entry, single confirmation prompt
        assert plan_calls == 1
        assert len(dropzone._pending_drop) == 1

        confirm_modal = tui.screen
        assert isinstance(confirm_modal, DropPlanConfirmModal)
        confirm_modal.query_one("#btn-confirm-drop-plan", Button).press()
        await _wait_for(
            tui,
            pilot,
            lambda: (
                not isinstance(tui.screen, DropZoneModal)
                and not isinstance(tui.screen, DropPlanConfirmModal)
            ),
        )
        await pilot.pause()

        # Single move execution pass & destination_dir returned from worker
        assert exec_calls == 1
        assert dropzone.processed_result is not None
        assert dropzone.processed_result["destination_dir"] == str(dest_dir.resolve())

        # Empty workspace base_dir AND source_dirs synced to destination_dir before scanning
        resolved_dest_str = str(dest_dir.resolve())
        assert tui.base_dir == resolved_dest_str
        assert tui.source_dirs == [resolved_dest_str]
        assert (resolved_dest_str, [resolved_dest_str]) in scan_snapshots

        # Cross-folder .md and .html files clustered into matching topic directories
        placed = {p.name: p.parent for p in dest_dir.rglob("*") if p.is_file()}
        assert set(placed.keys()) == {
            "synaptic_plasticity_cortex.md",
            "synaptic_neurotransmitter_axon.html",
            "photovoltaic_inverter_grid.md",
            "photovoltaic_solar_irradiance.txt",
        }
        assert (
            placed["synaptic_plasticity_cortex.md"]
            == placed["synaptic_neurotransmitter_axon.html"]
        )
        assert (
            placed["photovoltaic_inverter_grid.md"]
            == placed["photovoltaic_solar_irradiance.txt"]
        )

        # Wait for background scan worker to finish before undo
        await _wait_for(tui, pilot, lambda: not any(w.is_running for w in tui.workers))

        with acquire_services() as svc:
            sessions = svc.history.get_sessions()
            assert len(sessions) == 1

        # Single Ctrl+Z restores all 4 files across both dropped folders
        await pilot.press("ctrl+z")
        await _wait_for(
            tui, pilot, lambda: isinstance(tui.screen, RollbackConfirmModal)
        )
        tui.screen.query_one("#btn-confirm-rollback", Button).press()
        await _wait_for(
            tui,
            pilot,
            lambda: (
                neuro_md.exists()
                and neuro_html.exists()
                and solar_md.exists()
                and solar_txt.exists()
            ),
        )
        assert neuro_md.exists()
        assert neuro_html.exists()
        assert solar_md.exists()
        assert solar_txt.exists()
