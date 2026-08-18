"""Unit tests for the Unified Priority Overflow Toolbar System."""

from unittest.mock import MagicMock
from nicegui import Client, ui
from nicegui.elements.button import Button

from app.ui.toolbar import OverflowToolbar, ToolbarAction


def test_overflow_toolbar_creation_and_single_row_classes():
    with Client(None):
        toolbar = OverflowToolbar(title="Test Header")
        assert "overflow-toolbar-container" in toolbar._classes
        assert "flex-nowrap" in toolbar._classes
        assert "max-h-12" in toolbar._classes
        assert toolbar.actions == []


def test_add_action_returns_toolbar_action_with_priority_classes():
    with Client(None):
        toolbar = OverflowToolbar()
        cb = MagicMock()

        primary_action = toolbar.add_action(
            "Primary Action",
            on_click=cb,
            is_primary=True,
            priority=10,
            tooltip="Primary Tooltip",
            aria_label="Primary Aria",
        )

        secondary_action = toolbar.add_action(
            "Secondary Action",
            on_click=cb,
            is_primary=False,
            priority=0,
            tooltip="Secondary Tooltip",
            aria_label="Secondary Aria",
        )

        assert isinstance(primary_action, ToolbarAction)
        assert isinstance(primary_action.button, Button)
        assert primary_action.is_primary is True
        assert "overflow-toolbar-primary" in primary_action.button._classes

        assert isinstance(secondary_action, ToolbarAction)
        assert secondary_action.is_primary is False
        assert "overflow-toolbar-secondary" in secondary_action.button._classes

        assert len(toolbar.actions) == 2


def test_overflow_button_visibility_based_on_secondary_actions():
    with Client(None):
        toolbar = OverflowToolbar()

        # Adding primary action only -> overflow button hidden
        primary_action = toolbar.add_action("Primary", is_primary=True)
        assert "hidden" in toolbar.overflow_btn._classes

        # Adding secondary action -> overflow button becomes inline-flex
        secondary_action = toolbar.add_action("Secondary", is_primary=False)
        assert "inline-flex" in toolbar.overflow_btn._classes

        # Hiding secondary action -> overflow button becomes hidden again
        secondary_action.set_visibility(False)
        assert "hidden" in toolbar.overflow_btn._classes

        # Showing secondary action again -> overflow button becomes inline-flex
        secondary_action.set_visibility(True)
        assert "inline-flex" in toolbar.overflow_btn._classes


def test_state_passthrough_forwarding():
    with Client(None):
        toolbar = OverflowToolbar()
        action = toolbar.add_action("Action Test", is_primary=True)

        assert action.enabled is True
        action.disable()
        assert action.enabled is False
        action.enable()
        assert action.enabled is True

        assert action.visible is True
        action.set_visibility(False)
        assert action.visible is False

        action.set_text("Updated Text")
        assert action.text == "Updated Text"


def test_overflow_menu_population_and_callback_triggering():
    with Client(None):
        toolbar = OverflowToolbar()
        clicked = [False]

        def on_click():
            clicked[0] = True

        secondary_action = toolbar.add_action(
            "Secondary Action",
            on_click=on_click,
            icon="settings",
            is_primary=False,
            tooltip="Settings Tooltip",
            aria_label="Settings Button",
        )

        # Trigger population of menu items
        toolbar._populate_overflow_menu()

        # Find populated menu item
        items = list(toolbar.overflow_menu)
        assert len(items) == 1
        menu_item = items[0]
        assert any(getattr(c, "text", None) == "Secondary Action" for c in menu_item)

        # Trigger item click via toolbar helper
        toolbar._on_menu_item_click(secondary_action)
        assert clicked[0] is True


def test_dynamic_wizard_step_transitions():
    with Client(None):
        toolbar_welcome = OverflowToolbar()
        toolbar_download = OverflowToolbar()

        accept_btn = toolbar_welcome.add_action("Accept", is_primary=True)
        decline_btn = toolbar_welcome.add_action("Decline", is_primary=False)

        cancel_btn = toolbar_download.add_action("Cancel", is_primary=True)

        # Simulate transition: hide welcome, show download
        toolbar_welcome.set_visibility(False)
        toolbar_download.set_visibility(True)

        assert toolbar_welcome.visible is False
        assert toolbar_download.visible is True
        assert accept_btn.visible is True
        assert cancel_btn.visible is True
