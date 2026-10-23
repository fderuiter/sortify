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
