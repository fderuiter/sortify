"""Unit and integration tests for Centralized Native Semantic Heading Helper."""

import asyncio

import pytest

from app.config import AppSettings
from app.ui.heading import Heading, SemanticHeading, create_heading
from app.ui.tui import (
    AutoSorterTUI,
    CROForensicModal,
    DirectorySelectModal,
    DropZoneModal,
    ExportReportModal,
    HistoryModal,
    ModelManagerModal,
    NewFolderModal,
    QuarantineVaultModal,
    ReassignModal,
    RenameModal,
    SessionRecoveryModal,
    SettingsModal,
    ShortcutCheatSheetModal,
    TemplateGalleryModal,
    WizardModal,
    inspect_tui_component,
)


def test_heading_initialization_and_clamping():
    """Verify Heading initialization, level clamping, and alias equivalency."""
    h1 = Heading("Main Screen Title", level=1, id="title-h1", classes="modal-title")
    assert h1.level == 1
    assert h1.tag == "h1"
    assert h1.html_tag == "h1"
    assert h1.dom_tag == "h1"
    assert h1.role == "heading"
    assert h1.aria_level == "1"
    assert h1.dom_attributes == {
        "role": "heading",
        "aria-level": "1",
        "data-heading-level": "1",
        "data-tag": "h1",
    }

    # Test levels clamping
    h_low = Heading("Low Level", level=0)
    assert h_low.level == 1
    assert h_low.tag == "h1"

    h_high = Heading("High Level", level=99)
    assert h_high.level == 6
    assert h_high.tag == "h6"

    # Test SemanticHeading alias and create_heading helper
    h_alias = SemanticHeading("Section Header", level=2)
    assert isinstance(h_alias, Heading)
    assert h_alias.level == 2

    h_func = create_heading(
        "Card Header", level=3, id="card-3", classes="section-title"
    )
    assert h_func.level == 3
    assert h_func.tag == "h3"
    assert h_func.id == "card-3"


def test_heading_to_html_rendering():
    """Verify to_html() generates valid semantic HTML markup."""
    h = Heading(
        "Application Settings", level=1, id="settings-title", classes="modal-title"
    )
    html = h.to_html()
    assert (
        html
        == '<h1 role="heading" aria-level="1" id="settings-title" class="modal-title">Application Settings</h1>'
    )

    h2 = Heading("Sorting Strategy", level=2, classes="field-label")
    html2 = h2.to_html()
    assert (
        html2
        == '<h2 role="heading" aria-level="2" class="field-label">Sorting Strategy</h2>'
    )


def test_heading_reactivity():
    """Verify reactive updates for heading content and level."""
    h = Heading("Initial Header", level=1)
    assert h.level == 1
    assert h.tag == "h1"

    h.level = 2
    assert h.level == 2
    assert h.tag == "h2"
    assert h.aria_level == "2"

    h.update("Updated Reactive Title")
    assert "Updated Reactive Title" in h.to_html()


@pytest.fixture
def test_app_settings(tmp_path, monkeypatch):
    """Fixture providing isolated AppSettings."""
    import app.config

    monkeypatch.setattr(app.config, "get_app_dir", lambda: tmp_path)
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    return settings


def test_all_modals_heading_accessibility_compliance(test_app_settings, tmp_path):
    """Verify all modal screens use semantic Headings without accessibility violations."""
    modals = [
        RenameModal("Rename Test", "current", ".txt"),
        NewFolderModal(),
        ReassignModal(["FolderA", "FolderB"], count=2),
        DirectorySelectModal(str(tmp_path)),
        SettingsModal(test_app_settings),
        ModelManagerModal(test_app_settings),
        TemplateGalleryModal(test_app_settings),
        WizardModal(test_app_settings),
        SessionRecoveryModal(
            {"session_id": "s1", "base_dir": tmp_path, "status": "failed"}
        ),
        ShortcutCheatSheetModal(),
        DropZoneModal(test_app_settings, str(tmp_path)),
        ExportReportModal("./report.html"),
        HistoryModal([], str(tmp_path)),
        QuarantineVaultModal(base_dir=str(tmp_path)),
        CROForensicModal(test_app_settings, str(tmp_path)),
    ]

    async def _verify_modal(modal):
        app = AutoSorterTUI(settings=test_app_settings, base_dir=str(tmp_path))
        async with app.run_test() as pilot:
            app.push_screen(modal)
            await pilot.pause(0.05)

            # Audit accessibility
            audit_res = modal.audit_a11y_compliance()
            assert audit_res["compliant"] is True, (
                f"{type(modal).__name__} failed audit: {audit_res['violations']}"
            )

            violations = inspect_tui_component(modal)
            assert len(violations) == 0, (
                f"{type(modal).__name__} has violations: {violations}"
            )

            # Verify presence of Heading widget at level 1
            headings = [w for w in modal.query("*") if isinstance(w, Heading)]
            assert len(headings) >= 1, f"{type(modal).__name__} lacks Heading widgets"
            h1_headings = [h for h in headings if h.level == 1]
            assert len(h1_headings) == 1, (
                f"{type(modal).__name__} must have exactly one h1 Heading"
            )

    for m in modals:
        asyncio.run(_verify_modal(m))


def test_tui_main_screen_heading_accessibility(test_app_settings, tmp_path):
    """Verify main screen dual-pane view incorporates semantic Headings."""

    async def _test():
        app = AutoSorterTUI(settings=test_app_settings, base_dir=str(tmp_path))
        async with app.run_test() as pilot:
            await pilot.pause(0.05)

            audit_res = app.audit_a11y_compliance()
            assert audit_res["compliant"] is True

            headings = [w for w in app.query("*") if isinstance(w, Heading)]
            assert len(headings) >= 2, "Main screen should contain panel headings"
            meta_headings = [
                h for h in headings if "Node Metadata Inspector" in str(h.render())
            ]
            assert len(meta_headings) == 1
            assert meta_headings[0].level == 2

    asyncio.run(_test())
