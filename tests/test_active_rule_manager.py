"""Unit tests for ActiveRuleManagerModal, RuleEditModal, DeleteRuleConfirmModal, and rule navigation."""

import pytest
from textual.widgets import Button, DataTable, Input, Label

from app.config import AppSettings, Settings
from app.ui.modals.command_palette import DEFAULT_COMMAND_CATALOG
from app.ui.modals.rules import (
    ActiveRuleManagerModal,
    DeleteRuleConfirmModal,
    ExportRulesModal,
    RuleEditModal,
    RuleImportModal,
    TemplateGalleryModal,
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


@pytest.mark.anyio
async def test_active_rule_manager_renders_all_action_buttons(tmp_path):
    """Criterion 1: ActiveRuleManagerModal renders buttons for Close, Toggle Status, Delete Rule, Edit Rule, Add Rule, Import Rules, Export Rules, and Browse Templates."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    modal = ActiveRuleManagerModal(settings)
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(modal)
        await pilot.pause()

        expected_buttons = {
            "btn-close-rules": "Close",
            "btn-toggle-rule": "Toggle Status",
            "btn-delete-rule": "Delete Rule",
            "btn-edit-rule": "Edit Rule",
            "btn-add-rule": "Add Rule",
            "btn-import-rules": "Import Rules",
            "btn-export-rules": "Export Rules",
            "btn-browse-templates": "Browse Templates",
        }

        for btn_id, label in expected_buttons.items():
            btn = modal.query_one(f"#{btn_id}", Button)
            assert btn is not None
            assert str(btn.label) == label


@pytest.mark.anyio
async def test_active_rule_manager_import_rules_workflow(tmp_path):
    """Criterion 2: Completing an import workflow in RuleImportModal updates the rules table without closing the rule manager."""
    from app.core.rule_importers import ImportResult

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.KEYWORD_RULES = {"invoice": "Invoices"}
    modal = ActiveRuleManagerModal(settings)
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(modal)
        await pilot.pause()

        # Open import modal via button handler
        modal.action_import_rules()
        await pilot.pause()
        assert isinstance(app.screen, RuleImportModal)
        import_modal = app.screen

        # Simulate parsing rules into import modal
        import_modal.current_result = ImportResult(
            keyword_rules={"statement": "Statements", "receipt": "Receipts"},
            policies=[
                {
                    "type": "compliance",
                    "expression": "confidential",
                    "target_path": "Vault",
                    "priority": 10,
                    "enabled": True,
                }
            ],
            source_format="hazel",
        )

        # Merge imported rules
        import_modal.action_merge_rules()
        await pilot.pause()

        # Rule manager should remain active (not closed!)
        assert isinstance(app.screen, ActiveRuleManagerModal)
        table = modal.query_one("#rules-table", DataTable)
        patterns = [r["pattern"] for r in modal._rules_data]
        assert "invoice" in patterns
        assert "statement" in patterns
        assert "receipt" in patterns
        assert "confidential" in patterns
        assert table.row_count == 4


@pytest.mark.anyio
async def test_active_rule_manager_browse_templates_workflow(tmp_path):
    """Criterion 3: Applying template packs in TemplateGalleryModal updates the rules table immediately."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.KEYWORD_RULES = {"custom_kw": "CustomFolder"}
    modal = ActiveRuleManagerModal(settings)
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(modal)
        await pilot.pause()

        # Open template gallery
        modal.action_browse_templates()
        await pilot.pause()
        assert isinstance(app.screen, TemplateGalleryModal)
        gallery_modal = app.screen

        # Select legal_contracts template and apply
        gallery_modal.query_one("#chk-gallery-legal_contracts").value = True
        gallery_modal.action_apply_templates()
        await pilot.pause()

        # Rule manager remains active and table reloads immediately
        assert isinstance(app.screen, ActiveRuleManagerModal)
        patterns = [r["pattern"] for r in modal._rules_data]
        assert "custom_kw" in patterns
        assert "contract" in patterns
        assert "lease" in patterns


@pytest.mark.anyio
async def test_active_rule_manager_export_rules_modal_launch(tmp_path):
    """Criterion 4: ExportRulesModal opens from the rule manager with accurate rule count and returns to manager."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.KEYWORD_RULES = {"invoice": "Invoices", "taxes": "Taxes"}
    settings.POLICIES = [
        {
            "type": "compliance",
            "expression": "confidential",
            "target_path": "Vault",
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

        modal.action_export_rules()
        await pilot.pause()
        assert isinstance(app.screen, ExportRulesModal)
        export_modal = app.screen

        # Summary label should reflect 2 keyword rules and 1 policy
        summary_lbl = export_modal.query_one("#lbl-export-summary", Label)
        summary_text = str(summary_lbl.render())
        assert "2 active keyword rule(s)" in summary_text
        assert "1 policy rule(s)" in summary_text

        # Dismiss export modal
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ActiveRuleManagerModal)


@pytest.mark.anyio
async def test_active_rule_manager_keyboard_shortcuts(tmp_path):
    """Requirement 5: Keyboard shortcuts provide fast navigation to lifecycle actions."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.KEYWORD_RULES = {"invoice": "Invoices"}
    modal = ActiveRuleManagerModal(settings)
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(modal)
        await pilot.pause()

        # Test ctrl+i opens RuleImportModal
        await pilot.press("ctrl+i")
        await pilot.pause()
        assert isinstance(app.screen, RuleImportModal)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ActiveRuleManagerModal)

        # Test ctrl+e opens ExportRulesModal
        await pilot.press("ctrl+e")
        await pilot.pause()
        assert isinstance(app.screen, ExportRulesModal)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ActiveRuleManagerModal)

        # Test ctrl+b opens TemplateGalleryModal
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert isinstance(app.screen, TemplateGalleryModal)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ActiveRuleManagerModal)


@pytest.mark.anyio
async def test_active_rule_manager_accessibility_announcements(tmp_path):
    """Criterion 5: Screen readers announce button focus and modal transitions accurately."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    modal = ActiveRuleManagerModal(settings)
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(modal)
        await pilot.pause()

        # Focus Import Rules button
        btn_import = modal.query_one("#btn-import-rules", Button)
        btn_import.focus()
        await pilot.pause()

        messages = [a["message"] for a in modal.announcements]
        assert any("Import Rules button focused" in m for m in messages)

        # Focus Export Rules button
        btn_export = modal.query_one("#btn-export-rules", Button)
        btn_export.focus()
        await pilot.pause()

        messages = [a["message"] for a in modal.announcements]
        assert any("Export Rules button focused" in m for m in messages)

        # Focus Browse Templates button
        btn_browse = modal.query_one("#btn-browse-templates", Button)
        btn_browse.focus()
        await pilot.pause()

        messages = [a["message"] for a in modal.announcements]
        assert any("Browse Templates button focused" in m for m in messages)


@pytest.mark.anyio
async def test_active_rule_manager_responsive_layout_no_clip(tmp_path):
    """Requirement 6 & Constraints: Button rows do not cause horizontal wrapping or clip on 80-col terminal displays."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    modal = ActiveRuleManagerModal(settings)
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    # Test standard 80-column display
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.push_screen(modal)
        await pilot.pause()

        box = modal.query_one(".rule-manager-box")
        box_right = box.region.x + box.region.width

        for btn in modal.query(Button):
            btn_right = btn.region.x + btn.region.width
            # Each button must be positioned within the modal box
            assert btn_right <= box_right, (
                f"Button {btn.id} clips outside box right edge"
            )
            assert btn.region.x >= box.region.x, (
                f"Button {btn.id} clips outside box left edge"
            )
