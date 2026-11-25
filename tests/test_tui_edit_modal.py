"""Unit and integration tests for edit modal collision warning helper and delegation."""

import asyncio
from unittest.mock import MagicMock

import pytest
from textual.widgets import Label

from app.config import AppSettings
from app.ui.modals.edit import (
    ReassignModal,
    RenameModal,
    _set_collision_warning,
    _set_collision_warning_helper,
    _update_collision_warning,
)
from app.ui.tui import AutoSorterTUI

pytestmark = pytest.mark.xdist_group(name="tui")


class MockLabel:
    """Mock Label widget mimicking Textual Label update and CSS class manipulation."""

    def __init__(self, initial_classes: list[str] | None = None) -> None:
        self.text: str = ""
        self.classes: set[str] = set(initial_classes or [])

    def update(self, msg: str) -> None:
        self.text = msg

    def add_class(self, cls: str) -> None:
        self.classes.add(cls)

    def remove_class(self, cls: str) -> None:
        self.classes.discard(cls)

    def has_class(self, cls: str) -> bool:
        return cls in self.classes


class MockModal:
    """Mock modal screen that provides query_one behavior."""

    def __init__(self, label: MockLabel | None = None) -> None:
        self.label = label

    def query_one(self, selector: str, expect_type=None):
        if selector == "#warning-collision" and self.label is not None:
            return self.label
        raise RuntimeError(f"Widget matching '{selector}' not found.")


def test_collision_warning_helper_presentation():
    """Verify shared collision warning helper sets text and toggles modal-warning/modal-error classes."""
    lbl = MockLabel()
    modal = MockModal(label=lbl)

    # 1. Warning state (is_error=False)
    _set_collision_warning_helper(modal, "⚠️ Name already in use", is_error=False)
    assert lbl.text == "⚠️ Name already in use"
    assert "modal-warning" in lbl.classes
    assert "modal-error" not in lbl.classes

    # 2. Transition to Error state (is_error=True)
    _set_collision_warning_helper(
        modal, "⚠️ Security Error: Directory traversal", is_error=True
    )
    assert lbl.text == "⚠️ Security Error: Directory traversal"
    assert "modal-error" in lbl.classes
    assert "modal-warning" not in lbl.classes

    # 3. Transition back to Warning state (is_error=False)
    _set_collision_warning_helper(modal, "⚠️ File exists on disk", is_error=False)
    assert lbl.text == "⚠️ File exists on disk"
    assert "modal-warning" in lbl.classes
    assert "modal-error" not in lbl.classes

    # 4. Clear warning
    _set_collision_warning_helper(modal, "", is_error=False)
    assert lbl.text == ""
    assert "modal-warning" in lbl.classes
    assert "modal-error" not in lbl.classes


def test_collision_warning_helper_missing_widget_graceful():
    """Verify helper handles missing #warning-collision widget without throwing exceptions."""
    modal_without_label = MockModal(label=None)
    # Should catch exception internally and pass cleanly
    _set_collision_warning_helper(
        modal_without_label, "Ignored message", is_error=False
    )
    _set_collision_warning_helper(modal_without_label, "Ignored error", is_error=True)

    # Also verify aliases
    _set_collision_warning(modal_without_label, "Alias check", is_error=False)
    _update_collision_warning(modal_without_label, "Alias check", is_error=True)


def test_rename_modal_collision_warning_delegation():
    """Verify RenameModal._set_collision_warning delegates to the extracted helper."""
    plan = {
        "Docs": {
            "file.txt": {
                "__type__": "file",
                "filepath": "/test/file.txt",
                "target_filename": "file.txt",
            }
        }
    }
    modal = RenameModal(
        title="Rename File: doc.txt",
        current_name="doc",
        extension=".txt",
        category="Docs",
        plan=plan,
        base_dir="/test",
        source_path="/test/doc.txt",
    )
    lbl = MockLabel()
    modal.query_one = (  # type: ignore[method-assign]
        lambda selector, *args: lbl if selector == "#warning-collision" else None
    )

    # Warning state
    modal._set_collision_warning("Collision warning", is_error=False)
    assert lbl.text == "Collision warning"
    assert "modal-warning" in lbl.classes
    assert "modal-error" not in lbl.classes

    # Error state
    modal._set_collision_warning("Validation error", is_error=True)
    assert lbl.text == "Validation error"
    assert "modal-error" in lbl.classes
    assert "modal-warning" not in lbl.classes

    # Missing widget graceful
    modal.query_one = MagicMock(side_effect=RuntimeError("Widget not mounted"))  # type: ignore[method-assign]
    modal._set_collision_warning("Should not crash", is_error=False)
    modal._set_collision_warning("Should not crash", is_error=True)


def test_reassign_modal_collision_warning_delegation():
    """Verify ReassignModal._set_collision_warning delegates to the extracted helper."""
    modal = ReassignModal(
        existing_folders=["Archive"],
        count=1,
        plan={"Archive": {}},
        base_dir="/test",
        target_files=[
            {"key": "f.txt", "target_filename": "f.txt", "filepath": "/test/f.txt"}
        ],
    )
    lbl = MockLabel()
    modal.query_one = (  # type: ignore[method-assign]
        lambda selector, *args: lbl if selector == "#warning-collision" else None
    )

    # Warning state
    modal._set_collision_warning("Reassign collision", is_error=False)
    assert lbl.text == "Reassign collision"
    assert "modal-warning" in lbl.classes
    assert "modal-error" not in lbl.classes

    # Error state
    modal._set_collision_warning("Reassign security error", is_error=True)
    assert lbl.text == "Reassign security error"
    assert "modal-error" in lbl.classes
    assert "modal-warning" not in lbl.classes

    # Missing widget graceful
    modal.query_one = MagicMock(side_effect=RuntimeError("Widget not mounted"))  # type: ignore[method-assign]
    modal._set_collision_warning("Should not crash", is_error=False)
    modal._set_collision_warning("Should not crash", is_error=True)


def test_rename_modal_inline_warning_rendered():
    """Verify RenameModal renders the warning label widget and updates its classes in TUI pilot."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir="/workspace")

        async with app.run_test() as pilot:
            modal = RenameModal("Rename Test", "sample", ".txt")
            await app.push_screen(modal)
            await pilot.pause(0.1)

            warn_lbl = modal.query_one("#warning-collision", Label)
            assert warn_lbl is not None
            assert "modal-warning" in warn_lbl.classes

            # Trigger warning
            modal._set_collision_warning("Test warning in pilot", is_error=False)
            await pilot.pause(0.05)
            assert "modal-warning" in warn_lbl.classes
            assert "modal-error" not in warn_lbl.classes
            assert "Test warning in pilot" in str(warn_lbl.render())

            # Trigger error
            modal._set_collision_warning("Test error in pilot", is_error=True)
            await pilot.pause(0.05)
            assert "modal-error" in warn_lbl.classes
            assert "modal-warning" not in warn_lbl.classes
            assert "Test error in pilot" in str(warn_lbl.render())

            await modal.dismiss(None)

    asyncio.run(_test())
