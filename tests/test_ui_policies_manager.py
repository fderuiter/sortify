from unittest.mock import MagicMock, patch

from nicegui import Client

from app.config import AppSettings
from app.ui.settings import get_shadowed_policies, show_settings


def test_get_shadowed_policies():
    """Verify that get_shadowed_policies correctly identifies shadowed policies based on type and priority."""
    policies = [
        {"type": "keyword", "expression": "invoice", "target_path": "Invoices", "priority": 10},
        # Shadowed by "invoice" because ha_expr in lo_expr and priority 10 > 5
        {"type": "keyword", "expression": "invoice_overdue", "target_path": "Overdue", "priority": 5},
        # Not shadowed because priority 20 > 10
        {"type": "keyword", "expression": "invoice_final", "target_path": "Final", "priority": 20},
        # Pattern masked by keyword with higher priority
        {"type": "pattern", "expression": "final_invoice", "target_path": "FinalPattern", "priority": 5},
    ]

    shadowed = get_shadowed_policies(policies)
    assert shadowed == [False, True, False, True]


def test_get_shadowed_policies_tie_breaking():
    """Verify stable tie-breaking when priorities are equal (preserving original list order)."""
    policies = [
        {"type": "keyword", "expression": "invoice", "target_path": "First", "priority": 10},
        {"type": "keyword", "expression": "invoice_overdue", "target_path": "Second", "priority": 10},
    ]
    # Since they have equal priority, first one comes first in sorted order, so second one is shadowed
    shadowed = get_shadowed_policies(policies)
    assert shadowed == [False, True]


def test_policies_tab_and_validation():
    """Verify that the settings UI renders a separate Policies tab, and that validations block invalid paths."""
    notifications = []
    created_tabs = []
    created_tab_panels = []

    # Mock ui.notify
    def mock_notify(message, type="info", **kwargs):
        notifications.append((message, type))

    # Mock ui.tab and ui.tab_panel
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

    # Track inputs and buttons
    buttons = []
    inputs = {}

    def mock_button(text="", on_click=None, icon=None, **kwargs):
        btn = MagicMock()
        btn.text = text
        btn.icon = icon
        btn.on_click = on_click
        btn.classes = MagicMock(return_value=btn)
        btn.props = MagicMock(return_value=btn)
        btn.tooltip = MagicMock(return_value=btn)
        btn._event_listeners = {"click": MagicMock(handler=on_click, type="click")}
        buttons.append(btn)
        return btn

    def mock_input(label="", value="", **kwargs):
        inp = MagicMock()
        inp.label = label
        inp.value = value
        inp.classes = MagicMock(return_value=inp)
        inp.props = MagicMock(return_value=inp)
        inp.tooltip = MagicMock(return_value=inp)
        inputs[label] = inp
        return inp

    def mock_select(label="", options=None, value="", **kwargs):
        sel = MagicMock()
        sel.label = label
        sel.options = options
        sel.value = value
        sel.classes = MagicMock(return_value=sel)
        sel.props = MagicMock(return_value=sel)
        sel.tooltip = MagicMock(return_value=sel)
        inputs[label] = sel
        return sel

    def mock_number(label="", value=0, step=1, **kwargs):
        num = MagicMock()
        num.label = label
        num.value = value
        num.classes = MagicMock(return_value=num)
        num.props = MagicMock(return_value=num)
        num.tooltip = MagicMock(return_value=num)
        inputs[label] = num
        return num

    def mock_checkbox(label="", value=False, **kwargs):
        chk = MagicMock()
        chk.label = label
        chk.value = value
        chk.classes = MagicMock(return_value=chk)
        chk.props = MagicMock(return_value=chk)
        return chk

    # Standard setting initialization
    settings = AppSettings()
    settings.POLICIES = [
        {"type": "keyword", "expression": "tax", "target_path": "TaxDocs", "priority": 50},
        {"type": "keyword", "expression": "tax_2026", "target_path": "Tax2026", "priority": 10},
    ]

    parent_app = MagicMock()

    # Patch NiceGUI elements and methods to monitor settings layout building
    with (
        Client(None),
        patch("nicegui.ui.notify", side_effect=mock_notify),
        patch("nicegui.ui.tab", side_effect=mock_tab),
        patch("nicegui.ui.tab_panel", side_effect=mock_tab_panel),
        patch("nicegui.ui.button", side_effect=mock_button),
        patch("nicegui.ui.input", side_effect=mock_input),
        patch("nicegui.ui.select", side_effect=mock_select),
        patch("nicegui.ui.number", side_effect=mock_number),
        patch("nicegui.ui.checkbox", side_effect=mock_checkbox),
        patch("nicegui.ui.dialog", return_value=MagicMock()),
        patch("nicegui.ui.card", return_value=MagicMock()),
        patch("nicegui.ui.row", return_value=MagicMock()),
        patch("nicegui.ui.column", return_value=MagicMock()),
        patch("nicegui.ui.label", return_value=MagicMock()),
        patch("nicegui.ui.icon", return_value=MagicMock()),
    ):
        show_settings(parent_app, settings)

        # 1. Verify Policies tab is rendered
        assert any(name == "Policies" for name, _ in created_tabs), "Policies tab not found in created tabs"
        assert "Policies" in created_tab_panels, "Policies tab panel not created"

        # 2. Retrieve form fields
        print("INPUT KEYS CONFIG:", list(inputs.keys()))
        assert "Type" in inputs
        assert "Expression" in inputs
        assert "Target Path" in inputs
        assert "Priority" in inputs

        type_field = inputs["Type"]
        expr_field = inputs["Expression"]
        target_field = inputs["Target Path"]
        priority_field = inputs["Priority"]

        # Find Add Policy button
        add_btn = None
        for btn in buttons:
            if btn.text == "Add Policy":
                add_btn = btn
                break
        assert add_btn is not None, "Add Policy button not found"

        # Test Form Validation 1: Missing Fields
        type_field.value = "keyword"
        expr_field.value = ""
        target_field.value = "ValidFolder"
        priority_field.value = 10
        add_btn.on_click()
        assert any("Expression is required" in msg for msg, _ in notifications)

        notifications.clear()
        expr_field.value = "invoice"
        target_field.value = ""
        add_btn.on_click()
        assert any("Target Path is required" in msg for msg, _ in notifications)

        notifications.clear()
        target_field.value = "ValidFolder"
        priority_field.value = None
        add_btn.on_click()
        assert any("Priority is required" in msg for msg, _ in notifications)

        # Test Form Validation 2: Absolute Path Rejection
        notifications.clear()
        expr_field.value = "test"
        target_field.value = "/absolute/path/to/folder"
        priority_field.value = 10
        add_btn.on_click()
        assert any("cannot be an absolute path" in msg for msg, _ in notifications)

        notifications.clear()
        target_field.value = "\\absolute\\path\\to\\folder"
        add_btn.on_click()
        assert any("cannot be an absolute path" in msg for msg, _ in notifications)

        # Test Form Validation 3: Directory Traversal Rejection
        notifications.clear()
        target_field.value = "some/../traversal"
        add_btn.on_click()
        assert any("cannot contain directory traversal segments" in msg for msg, _ in notifications)

        # Test Form Validation 4: Illegal OS Characters Rejection
        notifications.clear()
        target_field.value = "Folder:Name"
        add_btn.on_click()
        assert any("contains illegal characters" in msg for msg, _ in notifications)

        notifications.clear()
        target_field.value = "Folder?Name"
        add_btn.on_click()
        assert any("contains illegal characters" in msg for msg, _ in notifications)

        # Test Form Validation 5: Successful Add
        notifications.clear()
        initial_count = len(settings.POLICIES)
        expr_field.value = "invoice"
        target_field.value = "InvoiceFolder"
        priority_field.value = 5
        print(f"BEFORE SUCCESSFUL ADD: type={type_field.value}, expr={expr_field.value}, target={target_field.value}, priority={priority_field.value}")
        add_btn.on_click()
        print(f"NOTIFICATIONS RECORDED: {notifications}")
        assert len(settings.POLICIES) == initial_count + 1
        assert settings.POLICIES[-1]["expression"] == "invoice"
        assert settings.POLICIES[-1]["target_path"] == "InvoiceFolder"
        assert any("Policy for 'invoice' added" in msg for msg, _ in notifications)
