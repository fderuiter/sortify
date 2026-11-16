"""Targeted test suite for edit modals to ensure high coverage without regressions."""

import pytest
from textual.app import App
from textual.widgets import Button, Input, Select, Static

from app.ui.modals.edit import (
    DirectorySelectModal,
    InteractiveCollisionResolverModal,
    ManualPathFallbackModal,
    NewFolderModal,
    ReassignModal,
    RenameModal,
    RenameResult,
    _format_file_size,
)


def test_rename_result_extended():
    res = RenameResult(
        "renamed.txt",
        destination_override="/tmp/override",
        batch_items=[{"a": 1}],
        is_batch=True,
    )
    assert res == "renamed.txt"
    assert res.name == "renamed.txt"
    assert res.destination_override == "/tmp/override"
    assert res.batch_items == [{"a": 1}]
    assert res.is_batch is True
    assert res.to_tuple() == ("renamed.txt", "/tmp/override")


def test_format_file_size_helper():
    assert _format_file_size(-1) == "0 B"
    assert _format_file_size(0) == "0 B"
    assert _format_file_size(500) == "500 B"
    assert _format_file_size(2048) == "2.0 KB"
    assert _format_file_size(1024 * 1024 * 5) == "5.0 MB"
    assert _format_file_size(1024 * 1024 * 1024 * 3) == "3.0 GB"
    assert _format_file_size(1024 * 1024 * 1024 * 1024 * 4) == "4.0 TB"


def test_directory_select_modal_validation(tmp_path):
    # Empty path
    assert (
        DirectorySelectModal._validate_directory_path("")
        == "Directory path cannot be empty."
    )
    assert (
        DirectorySelectModal._validate_directory_path("   ")
        == "Directory path cannot be empty."
    )

    # Non-existent
    non_existent = str(tmp_path / "does_not_exist")
    assert "does not exist" in (
        DirectorySelectModal._validate_directory_path(non_existent) or ""
    )

    # File path
    file_path = tmp_path / "file.txt"
    file_path.write_text("hello")
    assert "not a directory" in (
        DirectorySelectModal._validate_directory_path(str(file_path)) or ""
    )

    # Valid directory
    assert DirectorySelectModal._validate_directory_path(str(tmp_path)) is None


class ModalTestHostApp(App[None]):
    def __init__(self, modal_to_test):
        super().__init__()
        self.modal_to_test = modal_to_test
        self.modal_result = None

    def on_mount(self) -> None:
        def on_dismiss(res):
            self.modal_result = res

        self.push_screen(self.modal_to_test, on_dismiss)


@pytest.mark.anyio
async def test_manual_path_fallback_modal(tmp_path):
    modal = ManualPathFallbackModal(current_dir=str(tmp_path), reason="Test reason")
    app = ModalTestHostApp(modal)
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        # Check reason label
        reason_lbl = modal.query_one("#lbl-reason")
        assert "Test reason" in str(reason_lbl.render())

        # Test confirm button
        btn_confirm = modal.query_one("#btn-confirm-fallback", Button)
        btn_confirm.press()
        await pilot.pause(0.05)
        assert app.modal_result == str(tmp_path)


@pytest.mark.anyio
async def test_manual_path_fallback_modal_cancel(tmp_path):
    modal = ManualPathFallbackModal(current_dir=str(tmp_path))
    app = ModalTestHostApp(modal)
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        btn_cancel = modal.query_one("#btn-cancel-fallback", Button)
        btn_cancel.press()
        await pilot.pause(0.05)
        assert app.modal_result is None


@pytest.mark.anyio
async def test_manual_path_fallback_modal_empty_confirm():
    modal = ManualPathFallbackModal(current_dir="")
    app = ModalTestHostApp(modal)
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        inp = modal.query_one("#input-fallback-dir", Input)
        inp.value = ""
        modal.action_confirm()
        await pilot.pause(0.05)
        assert app.modal_result is None


@pytest.mark.anyio
async def test_new_folder_modal():
    modal = NewFolderModal()
    app = ModalTestHostApp(modal)
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        inp = modal.query_one("#input-folder-name", Input)
        inp.value = "NewCategory"
        modal.action_confirm()
        await pilot.pause(0.05)
        assert app.modal_result == "NewCategory"


@pytest.mark.anyio
async def test_new_folder_modal_cancel():
    modal = NewFolderModal()
    app = ModalTestHostApp(modal)
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        modal.action_cancel()
        await pilot.pause(0.05)
        assert app.modal_result is None


@pytest.mark.anyio
async def test_interactive_collision_resolver_modal(tmp_path):
    f1 = tmp_path / "colliding1.txt"
    f1.write_text("aaa")
    f2 = tmp_path / "colliding2.txt"
    f2.write_text("bbb")

    groups = [
        {
            "group_id": "g1",
            "target_filename": "output.txt",
            "target_dir": "Documents",
            "default_action": "rename",
            "items": [
                {"source_path": str(f1), "relative_source": "colliding1.txt"},
                {"source_path": str(f2), "relative_source": "colliding2.txt"},
            ],
        }
    ]
    modal = InteractiveCollisionResolverModal(duplicate_groups=groups)
    app = ModalTestHostApp(modal)
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        # Test responsiveness layout
        modal._update_layout(50)
        assert "narrow" in modal.classes
        modal._update_layout(80)
        assert "narrow" not in modal.classes

        # Test batch action apply to all
        batch_sel = modal.query_one("#batch-action-select", Select)
        batch_sel.value = "overwrite"
        btn_apply = modal.query_one("#btn-apply-all", Button)
        btn_apply.press()
        await pilot.pause(0.05)
        assert modal.user_selections["g1"] == "overwrite"

        # Change select directly
        sel_g1 = modal.query_one("#select-action-g1", Select)
        sel_g1.value = "skip"
        await pilot.pause(0.05)
        assert modal.user_selections["g1"] == "skip"

        # Confirm selections
        btn_confirm = modal.query_one("#btn-confirm-duplicates", Button)
        btn_confirm.press()
        await pilot.pause(0.05)
        assert app.modal_result == {"g1": "skip"}


@pytest.mark.anyio
async def test_interactive_collision_resolver_modal_cancel():
    modal = InteractiveCollisionResolverModal(duplicate_groups=[])
    app = ModalTestHostApp(modal)
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        btn_cancel = modal.query_one("#btn-cancel-duplicates", Button)
        btn_cancel.press()
        await pilot.pause(0.05)
        assert app.modal_result is None


@pytest.mark.anyio
async def test_directory_select_modal(tmp_path):
    sub1 = tmp_path / "sub1"
    sub1.mkdir()
    sub2 = tmp_path / "sub2"
    sub2.mkdir()

    modal = DirectorySelectModal(current_dir=str(sub1))
    app = ModalTestHostApp(modal)
    async with app.run_test() as pilot:
        await pilot.pause(0.05)

        # Test add dir with non-existent path -> shows error
        inp = modal.query_one("#input-dir", Input)
        inp.value = str(tmp_path / "not_there")
        btn_add = modal.query_one("#btn-add-dir", Button)
        btn_add.press()
        await pilot.pause(0.05)
        err_lbl = modal.query_one("#lbl-error", Static)
        assert "does not exist" in str(err_lbl.render())

        # Test input change clears error
        inp.value = str(sub2)
        await pilot.pause(0.05)
        assert str(err_lbl.render()) == ""

        # Test add valid dir
        btn_add.press()
        await pilot.pause(0.05)
        assert str(sub2) in modal.selected_sources

        # Test confirm with multiple selected sources
        btn_confirm = modal.query_one("#btn-confirm", Button)
        btn_confirm.press()
        await pilot.pause(0.05)
        assert isinstance(app.modal_result, list)
        assert str(sub1) in app.modal_result
        assert str(sub2) in app.modal_result


@pytest.mark.anyio
async def test_directory_select_modal_cancel():
    modal = DirectorySelectModal()
    app = ModalTestHostApp(modal)
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        btn_cancel = modal.query_one("#btn-cancel", Button)
        btn_cancel.press()
        await pilot.pause(0.05)
        assert app.modal_result is None


@pytest.mark.anyio
async def test_reassign_modal(tmp_path):
    modal = ReassignModal(existing_folders=["Archive", "Documents"], count=3)
    app = ModalTestHostApp(modal)
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        # Test collision warning styling
        modal._set_collision_warning("Warn msg", is_error=False)
        lbl = modal.query_one("#warning-collision")
        assert "modal-warning" in lbl.classes
        modal._set_collision_warning("Error msg", is_error=True)
        assert "modal-error" in lbl.classes

        # Select a folder from dropdown
        sel = modal.query_one("#select-folder", Select)
        sel.value = "Archive"
        await pilot.pause(0.05)

        # Confirm
        btn_confirm = modal.query_one("#btn-confirm", Button)
        btn_confirm.press()
        await pilot.pause(0.05)
        assert app.modal_result == "Archive"


@pytest.mark.anyio
async def test_reassign_modal_cancel():
    modal = ReassignModal(existing_folders=["Archive"], count=1)
    app = ModalTestHostApp(modal)
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        btn_cancel = modal.query_one("#btn-cancel", Button)
        btn_cancel.press()
        await pilot.pause(0.05)
        assert app.modal_result is None


def test_rename_modal_path_security():
    modal = RenameModal("Rename File", current_name="test.txt")
    assert modal._validate_path_security("") is None
    assert "Null bytes" in (modal._validate_path_security("bad\x00file.txt") or "")
    assert "Directory traversal" in (
        modal._validate_path_security("../secret/file.txt") or ""
    )
    assert "Directory traversal" in (
        modal._validate_path_security("folder/../file.txt") or ""
    )
    assert modal._validate_path_security("normal/nested/path.txt") is None
