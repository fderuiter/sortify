"""Unit tests for the Overflow Toolbar helper module."""

from unittest.mock import MagicMock

from app.ui.toolbar import OverflowToolbar, ToolbarAction


def test_overflow_toolbar_creation():
    toolbar = OverflowToolbar(title="Test Header")
    assert toolbar.actions == []


def test_add_action_returns_toolbar_action():
    toolbar = OverflowToolbar()
    cb = MagicMock()

    primary_action = toolbar.add_action(
        "Primary Action",
        on_click=cb,
        is_primary=True,
        priority=10,
    )

    assert isinstance(primary_action, ToolbarAction)
    assert primary_action.is_primary is True
    assert len(toolbar.actions) == 1
