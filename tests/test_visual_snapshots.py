"""Visual snapshot tests for Textual full-screen terminal user interface and modals."""

import asyncio
import os

import pytest

from app.config import AppSettings
from app.plugins.clinical_compliance.tui_views import CROForensicModal
from app.ui.tui import (
    AutoSorterTUI,
    DirectorySelectModal,
    ExtractionPreviewModal,
    NewFolderModal,
    RenameModal,
    SettingsModal,
    WizardModal,
)
from tests.svg_snapshot_harness import sanitize_svg, verify_svg_snapshot

pytestmark = [pytest.mark.xdist_group(name="tui")]

SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), "snapshots", "tui_svg")
# The Quit binding, which the footer shows in every snapshot.
FOOTER_MARKER = "&#160;^q&#160;"


def assert_svg_snapshot(snapshot_name: str, actual_svg: str) -> None:
    """Assert actual rendered SVG string matches stored reference snapshot file."""
    verify_svg_snapshot(snapshot_name, actual_svg, snapshot_dir=SNAPSHOT_DIR)


async def settled_screenshot(app, pilot, max_pauses: int = 100) -> str:
    """Take a screenshot once the app has finished rendering.

    Footer builds its key list in a later refresh than the first paint and
    rebuilds it when focus or bindings change, so a fixed number of pauses
    can catch it empty on a slow runner (seen on Windows CI), and so can a
    check on the footer's widgets alone. Wait for background workers, then
    for two screenshots in a row that match and show the footer's keys.
    """
    await app.workers.wait_for_complete()
    previous = None
    for _ in range(max_pauses):
        await pilot.pause(0.05)
        svg = app.export_screenshot()
        current = sanitize_svg(svg)
        if FOOTER_MARKER in current and current == previous:
            return svg
        previous = current
    pytest.fail("Screen never settled with the footer's key bindings drawn")


@pytest.fixture(autouse=True)
def isolated_app_dir(monkeypatch, tmp_path):
    """Ensure AppSettings is isolated from persistent disk configuration changes."""
    import app.config
    import app.core.session_catalog
    from app.core.plugin_registry import PluginRegistry
    from tests.conftest import reset_app_settings_observers

    PluginRegistry.reset_instance()
    reset_app_settings_observers()
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setattr(app.config, "get_app_dir", lambda: tmp_path)
    monkeypatch.setattr(
        app.config.AppSettings, "_trigger_save", lambda self: self._save()
    )
    monkeypatch.setattr(app.core.session_catalog, "find_abandoned_sessions", lambda: [])

    from textual.widgets import Input

    monkeypatch.setattr(Input, "cursor_blink", False)
    for k in list(os.environ.keys()):
        if k.startswith("AUTOSORTER_") and k != "AUTOSORTER_APP_DIR":
            monkeypatch.delenv(k, raising=False)

    try:
        from app.core.shared_registry import SharedModelRegistry

        reg = getattr(SharedModelRegistry, "_instance", None)
        if reg is not None:
            reg._cached_settings = None
        SharedModelRegistry._instance = None
    except Exception:
        pass

    if hasattr(app.config, "settings"):
        monkeypatch.delattr(app.config, "settings", raising=False)


def test_tui_main_screen_snapshot():
    """Verify visual layout of default AutoSorterTUI main screen."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)
        async with app.run_test(size=(100, 30)) as pilot:
            svg = await settled_screenshot(app, pilot)
            assert_svg_snapshot("tui_main_screen", svg)

    asyncio.run(_test())


def test_tui_populated_plan_snapshot():
    """Verify visual layout of AutoSorterTUI with a populated tree plan."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir="/dummy/workspace")
        async with app.run_test(size=(100, 30)) as pilot:
            app.plan = {
                "Finance": {
                    "q1_invoice.pdf": {
                        "__type__": "file",
                        "filepath": "/dummy/workspace/q1_invoice.pdf",
                        "target_filename": "q1_invoice.pdf",
                        "confidence": 0.98,
                        "category": "Finance",
                    },
                    "q2_budget.xlsx": {
                        "__type__": "file",
                        "filepath": "/dummy/workspace/q2_budget.xlsx",
                        "target_filename": "q2_budget.xlsx",
                        "confidence": 0.92,
                        "category": "Finance",
                    },
                },
                "Legal & Contracts": {
                    "vendor_agreement.docx": {
                        "__type__": "file",
                        "filepath": "/dummy/workspace/vendor_agreement.docx",
                        "target_filename": "vendor_agreement.docx",
                        "confidence": 0.89,
                        "category": "Legal",
                    }
                },
            }
            app.rebuild_tree()
            svg = await settled_screenshot(app, pilot)
            assert_svg_snapshot("tui_populated_plan", svg)

    asyncio.run(_test())


def test_wizard_modal_snapshot():
    """Verify visual layout of WizardModal onboarding screen."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(WizardModal(app.settings))
            svg = await settled_screenshot(app, pilot)
            assert_svg_snapshot("wizard_modal", svg)

    asyncio.run(_test())


def test_settings_modal_snapshot():
    """Verify visual layout of SettingsModal screen."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)
        async with app.run_test(size=(100, 35)) as pilot:
            modal = SettingsModal(app.settings)
            app.push_screen(modal)
            for _ in range(2):
                await pilot.pause()
            modal.query_one(".modal-box").scroll_home(animate=False)
            svg = await settled_screenshot(app, pilot)
            assert_svg_snapshot("settings_modal", svg)

    asyncio.run(_test())


def test_rename_modal_snapshot():
    """Verify visual layout of RenameModal screen."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)
        async with app.run_test(size=(100, 30)) as pilot:
            modal = RenameModal(
                title="Rename File: quarterly_report.pdf",
                current_name="quarterly_report",
                extension=".pdf",
            )
            app.push_screen(modal)
            svg = await settled_screenshot(app, pilot)
            assert_svg_snapshot("rename_modal", svg)

    asyncio.run(_test())


def test_cro_forensic_modal_snapshot():
    """Verify visual layout of CROForensicModal screen."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir="/dummy/study_root")
        async with app.run_test(size=(100, 35)) as pilot:
            modal = CROForensicModal(app.settings, base_dir=app.base_dir)
            app.push_screen(modal)
            for _ in range(2):
                await pilot.pause()
            modal.query_one(".modal-box").scroll_home(animate=False)
            svg = await settled_screenshot(app, pilot)
            assert_svg_snapshot("cro_forensic_modal", svg)

    asyncio.run(_test())


def test_new_folder_modal_snapshot():
    """Verify visual layout of NewFolderModal screen."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(NewFolderModal())
            svg = await settled_screenshot(app, pilot)
            assert_svg_snapshot("new_folder_modal", svg)

    asyncio.run(_test())


def test_directory_select_modal_snapshot():
    """Verify visual layout of DirectorySelectModal screen."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir="/dummy/projects")
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(DirectorySelectModal(current_dir=app.base_dir))
            svg = await settled_screenshot(app, pilot)
            assert_svg_snapshot("directory_select_modal", svg)

    asyncio.run(_test())


def test_extraction_preview_modal_snapshot(tmp_path):
    """Verify visual layout of ExtractionPreviewModal screen."""

    async def _test():
        from tests.test_extraction_preview import create_multi_page_pdf

        pdf_path = create_multi_page_pdf(tmp_path)
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)
        async with app.run_test(size=(100, 35)) as pilot:
            app.push_screen(ExtractionPreviewModal(pdf_path, app.settings))
            svg = await settled_screenshot(app, pilot)
            assert_svg_snapshot("extraction_preview_modal", svg)

    asyncio.run(_test())
