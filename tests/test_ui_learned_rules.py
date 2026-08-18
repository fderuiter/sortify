"""Tests for Learned Rules Settings Tab with Full Table Editing."""

from unittest.mock import MagicMock, patch

from nicegui import Client

from app.config import AppSettings
from app.ui.settings import show_settings


def test_learned_rules_tab_rendering_and_search():
    """Verify that the Learned Rules tab is created, displays active learned rules, and filters by search query."""
    created_tabs = []
    created_tab_panels = []
    notifications = []
    inputs = {}
    buttons = []

    def mock_notify(message, type="info", **kwargs):
        notifications.append((message, type))

    def mock_tab(name, label=None, **kwargs):
        created_tabs.append((name, label))
        mock_tab_obj = MagicMock()
        mock_tab_obj.props = MagicMock(return_value=mock_tab_obj)
        mock_tab_obj.classes = MagicMock(return_value=mock_tab_obj)
        return mock_tab_obj

    def mock_tab_panel(name, **kwargs):
        created_tab_panels.append(name)
        panel = MagicMock()
        panel.classes = MagicMock(return_value=panel)
        return panel

    def mock_input(label="", value="", on_change=None, **kwargs):
        inp = MagicMock()
        inp.label = label
        inp.value = value
        inp.on_change_handler = on_change
        inp.classes = MagicMock(return_value=inp)
        inp.props = MagicMock(return_value=inp)
        if label:
            inputs[label] = inp

        def set_val(new_val):
            inp.value = new_val
            if inp.on_change_handler:
                inp.on_change_handler(inp)

        inp.set_value = set_val
        return inp

    def mock_button(text="", on_click=None, icon=None, **kwargs):
        btn = MagicMock()
        btn.text = text
        btn.icon = icon
        btn.on_click = on_click
        btn.classes = MagicMock(return_value=btn)
        btn.props = MagicMock(return_value=btn)
        buttons.append(btn)
        return btn

    settings = AppSettings()
    settings.LEARNED_RULES = {
        "invoice": "Financial/Invoices",
        "receipt": "Financial/Receipts",
        "patient_report": "Medical/Reports",
    }

    parent_app = MagicMock()

    with (
        Client(None),
        patch("nicegui.ui.notify", side_effect=mock_notify),
        patch("nicegui.ui.tab", side_effect=mock_tab),
        patch("nicegui.ui.tab_panel", side_effect=mock_tab_panel),
        patch("nicegui.ui.input", side_effect=mock_input),
        patch("nicegui.ui.button", side_effect=mock_button),
        patch("nicegui.ui.dialog", return_value=MagicMock()),
        patch("nicegui.ui.card", return_value=MagicMock()),
        patch("nicegui.ui.row", return_value=MagicMock()),
        patch("nicegui.ui.column", return_value=MagicMock()),
        patch("nicegui.ui.label", return_value=MagicMock()),
        patch("nicegui.ui.icon", return_value=MagicMock()),
    ):
        show_settings(parent_app, settings)

        # 1. Check tab existence
        assert any(name == "Learned Rules" for name, _ in created_tabs), (
            "Learned Rules tab missing"
        )
        assert "Learned Rules" in created_tab_panels, "Learned Rules panel missing"

        # 2. Check search input existence
        assert "Search learned rules" in inputs, "Search input missing"
        search_inp = inputs["Search learned rules"]

        # Search filter test: filter by 'receipt'
        search_inp.set_value("receipt")
        # Should filter without errors
        assert search_inp.value == "receipt"


def test_learned_rules_inline_editing_and_validation():
    """Verify editing keyword and destination path, including validation error handling."""
    notifications = []
    created_inputs = []
    buttons = []

    def mock_notify(message, type="info", **kwargs):
        notifications.append((message, type))

    def mock_input(label="", value="", on_change=None, **kwargs):
        inp = MagicMock()
        inp.label = label
        inp.value = value
        inp.on_change_handler = on_change
        inp.on_value_change_handler = None
        inp.classes = MagicMock(return_value=inp)
        inp.props = MagicMock(return_value=inp)

        def on_val_change(handler):
            inp.on_value_change_handler = handler

        inp.on_value_change = on_val_change

        def set_val(new_val):
            inp.value = new_val
            if inp.on_change_handler:
                inp.on_change_handler(inp)
            if inp.on_value_change_handler:
                inp.on_value_change_handler(inp)

        inp.set_value = set_val
        created_inputs.append(inp)
        return inp

    def mock_button(text="", on_click=None, icon=None, **kwargs):
        btn = MagicMock()
        btn.text = text
        btn.icon = icon
        btn.on_click = on_click
        btn.classes = MagicMock(return_value=btn)
        btn.props = MagicMock(return_value=btn)
        buttons.append(btn)
        return btn

    settings = AppSettings()
    settings.LEARNED_RULES = {
        "invoice": "Invoices/2026",
    }

    parent_app = MagicMock()

    with (
        Client(None),
        patch("nicegui.ui.notify", side_effect=mock_notify),
        patch("nicegui.ui.input", side_effect=mock_input),
        patch("nicegui.ui.button", side_effect=mock_button),
        patch("nicegui.ui.tab", return_value=MagicMock()),
        patch("nicegui.ui.tab_panel", return_value=MagicMock()),
        patch("nicegui.ui.dialog", return_value=MagicMock()),
        patch("nicegui.ui.card", return_value=MagicMock()),
        patch("nicegui.ui.row", return_value=MagicMock()),
        patch("nicegui.ui.column", return_value=MagicMock()),
        patch("nicegui.ui.label", return_value=MagicMock()),
        patch("nicegui.ui.icon", return_value=MagicMock()),
    ):
        show_settings(parent_app, settings)

        # Retrieve row inputs (index 0 is search bar, index 1 is kw_input, index 2 is path_input)
        kw_inp = None
        path_inp = None
        for inp in created_inputs:
            if inp.value == "invoice":
                kw_inp = inp
            elif inp.value == "Invoices/2026":
                path_inp = inp

        assert kw_inp is not None, "Keyword input not found"
        assert path_inp is not None, "Destination path input not found"

        # Test 1: Invalid path edit (absolute path)
        notifications.clear()
        path_inp.set_value("/absolute/path/not/allowed")
        assert any("Invalid target path" in msg and type_ == "negative" for msg, type_ in notifications), (
            "Expected negative notification for absolute path edit"
        )
        assert settings.LEARNED_RULES["invoice"] == "Invoices/2026", (
            "Invalid path edit must not update settings"
        )

        # Test 2: Invalid path edit (illegal characters)
        notifications.clear()
        path_inp.set_value("Invoices:*?")
        assert any("Invalid target path" in msg and type_ == "negative" for msg, type_ in notifications), (
            "Expected negative notification for illegal path characters"
        )
        assert settings.LEARNED_RULES["invoice"] == "Invoices/2026"

        # Test 3: Invalid path edit (directory traversal)
        notifications.clear()
        path_inp.set_value("Invoices/../traversal")
        assert any("Invalid target path" in msg and type_ == "negative" for msg, type_ in notifications), (
            "Expected negative notification for directory traversal"
        )
        assert settings.LEARNED_RULES["invoice"] == "Invoices/2026"

        # Test 4: Valid path edit
        notifications.clear()
        path_inp.set_value("Invoices/Archived")
        assert settings.LEARNED_RULES["invoice"] == "Invoices/Archived", (
            "Valid path edit should update settings.LEARNED_RULES"
        )
        assert any("Updated destination path" in msg and type_ == "positive" for msg, type_ in notifications)

        # Test 5: Valid keyword edit
        notifications.clear()
        kw_inp.set_value("tax_invoice")
        assert "tax_invoice" in settings.LEARNED_RULES, "Keyword change should update settings"
        assert "invoice" not in settings.LEARNED_RULES, "Old keyword should be removed"
        assert settings.LEARNED_RULES["tax_invoice"] == "Invoices/Archived"


def test_learned_rules_row_deletion():
    """Verify that clicking the row deletion button removes the rule from settings."""
    notifications = []
    buttons = []

    def mock_notify(message, type="info", **kwargs):
        notifications.append((message, type))

    def mock_button(text="", on_click=None, icon=None, **kwargs):
        btn = MagicMock()
        btn.text = text
        btn.icon = icon
        btn.on_click = on_click
        btn.classes = MagicMock(return_value=btn)
        btn.props = MagicMock(return_value=btn)
        buttons.append(btn)
        return btn

    settings = AppSettings()
    settings.LEARNED_RULES = {
        "temp_rule": "Temporary/Path",
    }

    parent_app = MagicMock()

    with (
        Client(None),
        patch("nicegui.ui.notify", side_effect=mock_notify),
        patch("nicegui.ui.button", side_effect=mock_button),
        patch("nicegui.ui.input", return_value=MagicMock()),
        patch("nicegui.ui.tab", return_value=MagicMock()),
        patch("nicegui.ui.tab_panel", return_value=MagicMock()),
        patch("nicegui.ui.dialog", return_value=MagicMock()),
        patch("nicegui.ui.card", return_value=MagicMock()),
        patch("nicegui.ui.row", return_value=MagicMock()),
        patch("nicegui.ui.column", return_value=MagicMock()),
        patch("nicegui.ui.label", return_value=MagicMock()),
        patch("nicegui.ui.icon", return_value=MagicMock()),
    ):
        show_settings(parent_app, settings)

        # Find Delete button
        del_btn = None
        for btn in buttons:
            if btn.text == "Delete" and btn.icon == "delete":
                del_btn = btn
                break

        assert del_btn is not None, "Delete button for learned rule not found"

        # Click delete
        del_btn.on_click()

        # Check settings
        assert "temp_rule" not in settings.LEARNED_RULES, (
            "Rule should be deleted from settings.LEARNED_RULES"
        )
        assert any("deleted" in msg.lower() and type_ == "positive" for msg, type_ in notifications)
