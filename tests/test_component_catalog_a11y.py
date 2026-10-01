"""Unit tests for Component Catalog registry and accessibility scanner."""

from app.ui import catalog
from app.ui.a11y_runner import run_all_catalog_scans, scan_catalog_component
from app.ui.catalog import CATALOG_REGISTRY


def test_catalog_registry_populated():
    """Verify that CATALOG_REGISTRY contains all expected components and structure."""
    assert len(CATALOG_REGISTRY) >= 1
    for entry in CATALOG_REGISTRY:
        assert "id" in entry
        assert "name" in entry
        assert "render_func" in entry


def test_scan_catalog_component_passes_for_valid_components():
    """Verify that scan_catalog_component renders valid catalog components without violations."""
    for comp in CATALOG_REGISTRY:
        violations = scan_catalog_component(comp, "desktop", 1280)
        assert isinstance(violations, list)
        assert len(violations) == 0, (
            f"Unexpected violations for {comp['id']}: {violations}"
        )


def test_run_all_catalog_scans_executes_all_combos():
    """Verify that run_all_catalog_scans runs across all component state and viewport combos."""
    total_scans, violations = run_all_catalog_scans(CATALOG_REGISTRY)
    assert total_scans >= 32
    assert len(violations) == 0


def test_a11y_violation_detection_rule_a11y001_missing_label():
    """Verify detection of missing labels on interactive controls (A11Y001)."""

    def defective_render(container, state="default", viewport_width=1280):
        with catalog.ui.row():
            catalog.ui.button()

    entry = {
        "id": "defective_button",
        "name": "Defective Button",
        "render_func": defective_render,
    }

    violations = scan_catalog_component(entry, "desktop", 1280)
    assert len(violations) == 1
    assert violations[0].rule_id == "A11Y001_MISSING_LABEL"
    assert "ui.button" in violations[0].locator


def test_a11y_violation_detection_rule_a11y002_missing_alt():
    """Verify detection of missing alt attributes on images (A11Y002)."""

    def defective_render(container, state="default", viewport_width=1280):
        catalog.ui.image("logo.png")

    entry = {
        "id": "defective_image",
        "name": "Defective Image",
        "render_func": defective_render,
    }

    violations = scan_catalog_component(entry, "desktop", 1280)
    assert len(violations) == 1
    assert violations[0].rule_id == "A11Y002_MISSING_ALT"
    assert "ui.image" in violations[0].locator


def test_a11y_violation_detection_rule_a11y003_rigid_layout():
    """Verify detection of rigid layout bounds on narrow viewports (A11Y003)."""

    def defective_render(container, state="default", viewport_width=375):
        catalog.ui.card().classes("w-[800px]")

    entry = {
        "id": "defective_card",
        "name": "Defective Card",
        "render_func": defective_render,
    }

    violations = scan_catalog_component(entry, "mobile", 375)
    assert len(violations) == 1
    assert violations[0].rule_id == "A11Y003_RIGID_LAYOUT"
    assert "w-[800px]" in violations[0].message


def test_a11y_violation_detection_rule_a11y004_label_overflow():
    """Verify detection of unhandled label overflow on narrow viewports (A11Y004)."""

    def defective_render(container, state="default", viewport_width=375):
        catalog.ui.label(
            "This is a very long text string that stretches across the narrow viewport without wrapping classes"
        )

    entry = {
        "id": "defective_label",
        "name": "Defective Label",
        "render_func": defective_render,
    }

    violations = scan_catalog_component(entry, "mobile", 375)
    assert len(violations) == 1
    assert violations[0].rule_id == "A11Y004_LABEL_OVERFLOW"
    assert "375px" in violations[0].message


