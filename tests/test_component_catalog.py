"""Automated unit tests for Component Catalog CLI and accessibility gate."""

from app.ui.a11y import A11yViolation
from app.ui.catalog import get_catalog_registry, main


def test_catalog_registry_structure():
    """Verify CATALOG_REGISTRY defines active TUI screens and modals with required fields."""
    registry = get_catalog_registry()
    assert len(registry) >= 20

    comp_ids = [c["id"] for c in registry]
    assert "autosorter_tui" in comp_ids
    assert "rename_modal" in comp_ids
    assert "settings_modal" in comp_ids
    assert "wizard_modal" in comp_ids
    assert "command_palette_modal" in comp_ids
    assert "sample_playground_modal" in comp_ids
    assert "cro_forensic_modal" in comp_ids

    for entry in registry:
        assert "id" in entry
        assert "name" in entry
        assert "category" in entry
        assert "description" in entry
        assert "factory" in entry
        assert callable(entry["factory"])


def test_catalog_cli_list(capsys):
    """Verify --list flag prints registered components and exits with 0."""
    code = main(["--list"])
    assert code == 0
    captured = capsys.readouterr()
    assert "Registered Textual TUI Component Catalog" in captured.out
    assert "autosorter_tui" in captured.out
    assert "rename_modal" in captured.out


def test_catalog_cli_audit_only_all():
    """Verify --audit-only scans all registered TUI components without exceptions and passes."""
    code = main(["--audit-only"])
    assert code == 0


def test_catalog_cli_audit_only_single_component():
    """Verify --audit-only --component <id> scans specified component."""
    code = main(["--audit-only", "--component", "rename_modal"])
    assert code == 0


def test_catalog_cli_audit_only_unknown_component(capsys):
    """Verify --audit-only with unknown component returns exit code 1."""
    code = main(["--audit-only", "--component", "invalid_modal_xyz"])
    assert code == 1
    captured = capsys.readouterr()
    assert (
        "Unknown component identifier" in captured.err
        or "Unknown component identifier" in captured.out
    )


def test_catalog_audit_detects_accessibility_violations(monkeypatch):
    """Verify --audit-only returns exit code 1 when accessibility violations are detected."""
    fake_violation = A11yViolation(
        rule_id="A11Y001_MISSING_LABEL",
        component_id="rename_modal",
        component_name="RenameModal",
        viewport_name="terminal",
        viewport_width=80,
        locator="btn-submit",
        message="Button lacks explicit tooltip or label",
    )

    def mock_inspect(component):
        return [fake_violation]

    monkeypatch.setattr("app.ui.catalog.inspect_tui_component", mock_inspect)

    code = main(["--audit-only", "--component", "rename_modal"])
    assert code == 1


def test_unlabeled_input_triggers_missing_label_violation():
    """Verify Input controls with value but no label/tooltip/placeholder trigger A11Y001_MISSING_LABEL."""
    from textual.app import App
    from textual.screen import ModalScreen
    from textual.widgets import Input

    from app.ui.a11y import A11yMixin

    class UnlabeledInputModal(A11yMixin, ModalScreen[None]):
        def compose(self):
            yield Input(value="sample_val", id="unlabeled-inp")

    app = App()

    async def _run():
        async with app.run_test() as pilot:
            modal = UnlabeledInputModal()
            app.push_screen(modal)
            await pilot.pause(0.01)
            result = modal.audit_a11y_compliance()
            missing_labels = [
                v
                for v in result.get("violations", [])
                if v.get("rule") == "A11Y001_MISSING_LABEL"
                and v.get("widget_id") == "unlabeled-inp"
            ]
            assert len(missing_labels) == 1
            assert (
                "lacks explicit tooltip or accessible text label"
                in missing_labels[0]["message"]
            )

    import asyncio

    asyncio.run(_run())


def test_select_prompt_satisfies_accessible_label():
    """Verify Select controls with prompt attribute satisfy accessible label validation."""
    from textual.app import App
    from textual.screen import ModalScreen
    from textual.widgets import Select

    from app.ui.a11y import A11yMixin

    class PromptSelectModal(A11yMixin, ModalScreen[None]):
        def compose(self):
            yield Select(
                [("Opt 1", "1")], prompt="Choose an option...", id="prompt-select"
            )

    app = App()

    async def _run():
        async with app.run_test() as pilot:
            modal = PromptSelectModal()
            app.push_screen(modal)
            await pilot.pause(0.01)
            result = modal.audit_a11y_compliance()
            missing_labels = [
                v
                for v in result.get("violations", [])
                if v.get("rule") == "A11Y001_MISSING_LABEL"
                and v.get("widget_id") == "prompt-select"
            ]
            assert len(missing_labels) == 0

    import asyncio

    asyncio.run(_run())


def test_extraction_preview_input_threshold_accessible_attributes():
    """Verify preview-input-threshold widget in ExtractionPreviewModal has explicit tooltip and label."""
    from textual.app import App
    from textual.widgets import Input

    from app.config import AppSettings
    from app.ui.modals.reports import ExtractionPreviewModal

    app = App()

    async def _run():
        async with app.run_test() as pilot:
            modal = ExtractionPreviewModal(
                "/tmp/nonexistent.pdf", settings=AppSettings()
            )
            app.push_screen(modal)
            await pilot.pause(0.01)
            inp = modal.query_one("#preview-input-threshold", Input)
            assert bool(getattr(inp, "tooltip", None)) is True
            assert (
                bool(getattr(inp, "label", None) or getattr(inp, "placeholder", None))
                is True
            )
            result = modal.audit_a11y_compliance()
            threshold_violations = [
                v
                for v in result.get("violations", [])
                if v.get("rule") == "A11Y001_MISSING_LABEL"
                and v.get("widget_id") == "preview-input-threshold"
            ]
            assert len(threshold_violations) == 0

    import asyncio

    asyncio.run(_run())
