"""Unit tests for ActiveRuleManagerModal, RuleEditModal, DeleteRuleConfirmModal, and rule navigation."""

import pytest
from textual.widgets import Button, DataTable, Input, Label

from app.config import AppSettings, Settings
from app.ui.modals.command_palette import DEFAULT_COMMAND_CATALOG
from app.ui.modals.rules import (
    ActiveRuleManagerModal,
    DeleteRuleConfirmModal,
    RuleEditModal,
)
from app.ui.modals.settings import SettingsModal
from app.ui.tui import AutoSorterTUI


@pytest.fixture(autouse=True)
def mock_abandoned_sessions(monkeypatch):
    """Prevent session recovery modals from interrupting TUI tests."""
    import app.core.session_catalog

    monkeypatch.setattr(
        app.core.session_catalog, "find_abandoned_sessions", lambda *a, **k: []
    )


def test_disabled_keyword_rules_schema_validation(tmp_path):
    """Verify Settings and AppSettings validate and persist DISABLED_KEYWORD_RULES."""
    settings_file = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(settings_file))

    # Valid relative path should succeed
    settings.DISABLED_KEYWORD_RULES = {"invoice": "Financial/Invoices"}
    settings.flush()
    assert settings.DISABLED_KEYWORD_RULES == {"invoice": "Financial/Invoices"}

    # Invalid paths should raise ValueError
    with pytest.raises(ValueError):
        settings.DISABLED_KEYWORD_RULES = {"invoice": "/absolute/path"}

    with pytest.raises(ValueError):
        settings.DISABLED_KEYWORD_RULES = {"invoice": "../outside/path"}


@pytest.mark.anyio
async def test_active_rule_manager_displays_rules_and_policies(tmp_path):
    """Verify DataTable displays active keyword rules, disabled keyword rules, and policies."""
    settings_file = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(settings_file))
    settings.KEYWORD_RULES = {"invoice": "Invoices", "receipt": "Receipts"}
    settings.DISABLED_KEYWORD_RULES = {"contract": "Legal/Contracts"}
    settings.POLICIES = [
        {
            "type": "compliance",
            "expression": "confidential",
            "target_path": "Compliance/Vault",
            "priority": 10,
            "enabled": True,
        },
        {
            "type": "pattern",
            "expression": "draft",
            "target_path": "Archive/Drafts",
            "priority": 5,
            "enabled": False,
        },
    ]

    modal = ActiveRuleManagerModal(settings)
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(modal)
        await pilot.pause()

        table = modal.query_one("#rules-table", DataTable)
        assert table.row_count == 5

        # Inspect table rows data
        rules_data = modal._rules_data
        patterns = [r["pattern"] for r in rules_data]
        statuses = [r["status"] for r in rules_data]
        assert "invoice" in patterns
        assert "receipt" in patterns
        assert "contract" in patterns
        assert "confidential" in patterns
        assert "draft" in patterns

        contract_idx = patterns.index("contract")
        assert statuses[contract_idx] == "Disabled"

        draft_idx = patterns.index("draft")
        assert statuses[draft_idx] == "Disabled"

        confidential_idx = patterns.index("confidential")
        assert statuses[confidential_idx] == "Active"


@pytest.mark.anyio
async def test_toggle_keyword_rule_and_preserve_focus(tmp_path):
    """Verify toggling moves keyword rule between KEYWORD_RULES and DISABLED_KEYWORD_RULES and preserves focus."""
    settings_file = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(settings_file))
    settings.KEYWORD_RULES = {"invoice": "Invoices"}
    settings.DISABLED_KEYWORD_RULES = {}

    modal = ActiveRuleManagerModal(settings)
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(modal)
        await pilot.pause()

        table = modal.query_one("#rules-table", DataTable)
        table.focus()
        assert table.has_focus

        # Initially active
        assert "invoice" in settings.KEYWORD_RULES
        assert "invoice" not in settings.DISABLED_KEYWORD_RULES

        # Press Toggle button
        modal.action_toggle_rule()
        await pilot.pause()

        # Should now be disabled
        assert "invoice" not in settings.KEYWORD_RULES
        assert "invoice" in settings.DISABLED_KEYWORD_RULES
        assert settings.DISABLED_KEYWORD_RULES["invoice"] == "Invoices"

        # Focus should be preserved on table
        assert table.has_focus

        # Toggle back to active
        modal.action_toggle_rule()
        await pilot.pause()

        assert "invoice" in settings.KEYWORD_RULES
        assert "invoice" not in settings.DISABLED_KEYWORD_RULES
        assert table.has_focus


@pytest.mark.anyio
async def test_toggle_policy_status(tmp_path):
    """Verify toggling a policy flips enabled boolean."""
    settings_file = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(settings_file))
    settings.KEYWORD_RULES = {}
    settings.DISABLED_KEYWORD_RULES = {}
    settings.POLICIES = [
        {
            "type": "compliance",
            "expression": "confidential",
            "target_path": "Compliance/Vault",
            "priority": 10,
            "enabled": True,
        }
    ]

    modal = ActiveRuleManagerModal(settings)
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(modal)
        await pilot.pause()

        # Initially active
        assert settings.POLICIES[0]["enabled"] is True

        modal.action_toggle_rule()
        await pilot.pause()

        # Now disabled
        assert settings.POLICIES[0]["enabled"] is False

        modal.action_toggle_rule()
        await pilot.pause()

        assert settings.POLICIES[0]["enabled"] is True


@pytest.mark.anyio
async def test_rule_editor_modal_path_validation(tmp_path):
    """Verify RuleEditModal validates target folder using validate_target_path before saving."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        editor = RuleEditModal(title="Add Keyword Rule")
        app.push_screen(editor)
        await pilot.pause()

        inp_pattern = editor.query_one("#input-rule-pattern", Input)
        inp_target = editor.query_one("#input-rule-target", Input)
        err_lbl = editor.query_one("#lbl-rule-error", Label)

        # Case 1: Empty pattern
        editor.action_save()
        await pilot.pause()
        assert "empty" in str(err_lbl.render()).lower()
        assert isinstance(app.screen, RuleEditModal)

        # Case 2: Invalid absolute path
        inp_pattern.value = "invoice"
        inp_target.value = "/absolute/not/allowed"
        editor.action_save()
        await pilot.pause()
        assert "absolute" in str(err_lbl.render()).lower()
        assert isinstance(app.screen, RuleEditModal)

        # Case 3: Invalid directory traversal path
        inp_target.value = "../traversal/path"
        editor.action_save()
        await pilot.pause()
        assert "traversal" in str(err_lbl.render()).lower()
        assert isinstance(app.screen, RuleEditModal)

        # Case 4: Valid relative path
        inp_target.value = "Finances/Invoices"
        editor.action_save()
        await pilot.pause()
        # Modal should be dismissed
        assert not isinstance(app.screen, RuleEditModal)


@pytest.mark.anyio
async def test_add_and_edit_rule_flow(tmp_path):
    """Verify adding and editing a keyword rule persists changes to settings."""
    settings_file = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(settings_file))
    settings.KEYWORD_RULES = {"invoice": "Invoices"}

    modal = ActiveRuleManagerModal(settings)
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(modal)
        await pilot.pause()

        # Test add rule
        modal.action_add_rule()
        await pilot.pause()
        assert isinstance(app.screen, RuleEditModal)
        editor = app.screen
        editor.query_one("#input-rule-pattern", Input).value = "taxes"
        editor.query_one("#input-rule-target", Input).value = "Finances/Taxes"
        editor.action_save()
        await pilot.pause()

        assert "taxes" in settings.KEYWORD_RULES
        assert settings.KEYWORD_RULES["taxes"] == "Finances/Taxes"

        # Test edit rule: select taxes row and edit
        table = modal.query_one("#rules-table", DataTable)
        patterns = [r["pattern"] for r in modal._rules_data]
        taxes_idx = patterns.index("taxes")
        table.move_cursor(row=taxes_idx)

        modal.action_edit_rule()
        await pilot.pause()
        assert isinstance(app.screen, RuleEditModal)
        edit_modal = app.screen
        assert edit_modal.query_one("#input-rule-pattern", Input).value == "taxes"
        edit_modal.query_one("#input-rule-target", Input).value = "Finances/2026_Taxes"
        edit_modal.action_save()
        await pilot.pause()

        assert settings.KEYWORD_RULES["taxes"] == "Finances/2026_Taxes"


@pytest.mark.anyio
async def test_delete_rule_requires_confirmation(tmp_path):
    """Verify deleting a rule requires confirmation modal and only deletes when confirmed."""
    settings_file = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(settings_file))
    settings.KEYWORD_RULES = {"invoice": "Invoices", "contract": "Contracts"}

    modal = ActiveRuleManagerModal(settings)
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(modal)
        await pilot.pause()

        table = modal.query_one("#rules-table", DataTable)
        table.move_cursor(row=0)
        target_rule = modal._get_selected_rule()
        target_pattern = target_rule["pattern"]

        # Press delete: triggers confirmation modal
        modal.action_delete_rule()
        await pilot.pause()
        assert isinstance(app.screen, DeleteRuleConfirmModal)
        confirm_modal = app.screen

        # Test Cancel: rule should NOT be deleted
        confirm_modal.action_cancel_button()
        await pilot.pause()
        assert target_pattern in settings.KEYWORD_RULES

        # Press delete again and Confirm
        modal.action_delete_rule()
        await pilot.pause()
        assert isinstance(app.screen, DeleteRuleConfirmModal)
        confirm_modal = app.screen
        confirm_modal.action_confirm_delete()
        await pilot.pause()

        assert target_pattern not in settings.KEYWORD_RULES


@pytest.mark.anyio
async def test_command_palette_navigation_to_rule_manager(tmp_path):
    """Verify command palette includes Open Active Rule Manager and launches it."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    cmd_ids = [c.id for c in DEFAULT_COMMAND_CATALOG]
    assert "open_active_rules" in cmd_ids
    cmd = next(c for c in DEFAULT_COMMAND_CATALOG if c.id == "open_active_rules")
    assert cmd.shortcut == "Ctrl+R"
    assert cmd.action_name == "action_open_active_rules"

    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_active_rules()
        await pilot.pause()
        assert isinstance(app.screen, ActiveRuleManagerModal)


@pytest.mark.anyio
async def test_settings_dialog_button_navigation(tmp_path):
    """Verify settings modal contains button to launch Active Rule Manager."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        settings_modal = SettingsModal(settings)
        app.push_screen(settings_modal)
        await pilot.pause()

        btn_rules = settings_modal.query_one("#btn-open-rules", Button)
        assert btn_rules is not None

        # Press button
        settings_modal.action_open_active_rules()
        await pilot.pause()
        assert isinstance(app.screen, ActiveRuleManagerModal)


@pytest.mark.anyio
async def test_ctrl_r_shortcut_opens_rule_manager(tmp_path):
    """Verify Ctrl+R keybinding opens ActiveRuleManagerModal."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    # Verify class binding
    ctrl_r_bindings = [b for b in AutoSorterTUI.BINDINGS if b.key == "ctrl+r"]
    assert len(ctrl_r_bindings) == 1
    assert ctrl_r_bindings[0].action == "open_active_rules"

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert isinstance(app.screen, ActiveRuleManagerModal)


def test_disabled_rules_excluded_from_sorting_metadata_pass(tmp_path):
    """Verify disabled keyword rules and disabled policies are excluded during sorting evaluation."""
    from app.core.policy_engine import PolicyEngine

    settings = Settings(
        KEYWORD_RULES={"active_kw": "ActiveFolder"},
        DISABLED_KEYWORD_RULES={"disabled_kw": "DisabledFolder"},
        POLICIES=[
            {
                "type": "keyword",
                "expression": "active_pol",
                "target_path": "ActivePolicyFolder",
                "priority": 10,
                "enabled": True,
            },
            {
                "type": "keyword",
                "expression": "disabled_pol",
                "target_path": "DisabledPolicyFolder",
                "priority": 20,
                "enabled": False,
            },
        ],
    )

    # PolicyEngine evaluation directly
    res = PolicyEngine.evaluate_policies(
        file_path="test_disabled_pol.txt",
        doc_text="some text with disabled_pol",
        status_match=None,
        policies=settings.POLICIES,
    )
    # The higher-priority disabled_pol rule should be ignored
    assert res is None

    # Now with active_pol present
    res_active = PolicyEngine.evaluate_policies(
        file_path="test_active_pol.txt",
        doc_text="some text with active_pol and disabled_pol",
        status_match=None,
        policies=settings.POLICIES,
    )
    assert res_active is not None
    assert res_active.expression == "active_pol"
    assert res_active.target_path == "ActivePolicyFolder"
