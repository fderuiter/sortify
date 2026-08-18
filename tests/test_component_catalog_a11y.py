"""Unit and integration tests for Component Catalog and Accessibility Gate."""

import subprocess
import sys

from nicegui import ui

from app.ui.a11y_runner import (
    DEFAULT_CONFIGURED_VIEWPORTS,
    run_all_catalog_scans,
    scan_catalog_component,
)
from app.ui.catalog import CATALOG_REGISTRY


def test_catalog_registry_populated():
    """Verify that CATALOG_REGISTRY contains all expected components and structure."""
    assert len(CATALOG_REGISTRY) >= 8
    expected_ids = {
        "header_bar",
        "directory_selection",
        "plan_treeview",
        "settings_modal",
        "setup_wizard",
        "cro_forensic_dialog",
        "help_modal",
        "status_progress_panel",
    }
    registered_ids = {c["id"] for c in CATALOG_REGISTRY}
    assert expected_ids.issubset(registered_ids)

    for entry in CATALOG_REGISTRY:
        assert "id" in entry
        assert "name" in entry
        assert "description" in entry
        assert "render_func" in entry
        assert callable(entry["render_func"])
        assert "sample_states" in entry


def test_a11y_gate_passes_for_all_catalog_components():
    """Verify that all registered catalog components pass accessibility & layout scans across viewports."""
    total_scans, violations = run_all_catalog_scans(
        CATALOG_REGISTRY, viewports=DEFAULT_CONFIGURED_VIEWPORTS
    )
    assert total_scans >= 32
    assert len(violations) == 0, f"Expected 0 A11y violations, got {len(violations)}: {violations}"


def test_a11y_gate_catches_missing_label_violation():
    """Verify that the accessibility scanner detects missing labels/aria-labels on buttons."""

    def render_bad_button(container, state="default", viewport_width=1280):
        ui.button(icon="close").props("flat round")  # No text, label, or aria-label

    bad_entry = {
        "id": "bad_button_comp",
        "name": "Bad Button Component",
        "description": "Component with unlabeled button",
        "render_func": render_bad_button,
    }

    violations = scan_catalog_component(bad_entry, "desktop", 1280)
    assert len(violations) > 0
    v = violations[0]
    assert v.rule_id == "A11Y001_MISSING_LABEL"
    assert v.component_id == "bad_button_comp"
    assert "ui.button" in v.locator
    assert "lacks an explicit text label" in v.message


def test_a11y_gate_catches_rigid_layout_overflow_violation():
    """Verify that the scanner catches rigid pixel width classes on narrow viewports."""

    def render_rigid_card(container, state="default", viewport_width=320):
        ui.card().classes("w-[800px] p-4 bg-white")

    rigid_entry = {
        "id": "rigid_card_comp",
        "name": "Rigid Card Component",
        "description": "Component with rigid width exceeding narrow viewport",
        "render_func": render_rigid_card,
    }

    violations = scan_catalog_component(rigid_entry, "narrow_mobile", 320)
    assert len(violations) > 0
    rule_ids = [v.rule_id for v in violations]
    assert "A11Y003_RIGID_LAYOUT" in rule_ids
    v = [v for v in violations if v.rule_id == "A11Y003_RIGID_LAYOUT"][0]
    assert "w-[800px]" in v.message


def test_a11y_gate_catches_label_overflow_violation():
    """Verify that the scanner catches long un-wrapped label text on narrow viewports."""

    def render_overflow_text(container, state="default", viewport_width=320):
        ui.label(
            "This is an extraordinarily long text string without flex-wrap or truncation classes on a 320px viewport"
        )

    overflow_entry = {
        "id": "overflow_text_comp",
        "name": "Overflow Text Component",
        "description": "Component with long un-wrapped label text",
        "render_func": render_overflow_text,
    }

    violations = scan_catalog_component(overflow_entry, "narrow_mobile", 320)
    assert len(violations) > 0
    rule_ids = [v.rule_id for v in violations]
    assert "A11Y004_LABEL_OVERFLOW" in rule_ids


def test_cli_a11y_gate_script():
    """Test running scripts/run_a11y_gate.py as a subprocess."""
    result = subprocess.run(
        [sys.executable, "scripts/run_a11y_gate.py"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "SUCCESS: All" in result.stdout
    assert "accessibility scans passed!" in result.stdout


def test_cli_standalone_catalog_audit_mode():
    """Test running scripts/component_catalog.py --audit-only as a subprocess."""
    result = subprocess.run(
        [sys.executable, "scripts/component_catalog.py", "--audit-only"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "SUCCESS: All" in result.stdout
