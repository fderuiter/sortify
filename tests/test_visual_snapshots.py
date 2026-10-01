"""Visual snapshot tests for Textual full-screen terminal user interface and modals."""

import asyncio
import os
import re
from unittest.mock import AsyncMock

import pytest

from app.config import AppSettings
from app.ui.tui import (
    AutoSorterTUI,
    CROForensicModal,
    DirectorySelectModal,
    NewFolderModal,
    RenameModal,
    SettingsModal,
    WizardModal,
)

pytestmark = pytest.mark.xdist_group(name="tui")

SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), "snapshots", "tui_svg")


def sanitize_svg(svg: str) -> str:
    """Sanitize and canonicalize dynamic elements in Textual rendered SVG output.

    Normalizes Rich's auto-generated unique element ID prefixes, clock timestamps,
    Windows path separators, drive letters, filters out unused CSS style declarations,
    and sorts active style declarations to ensure deterministic baseline snapshot
    comparisons across operating systems and test execution order.
    """
    svg = svg.replace("\r\n", "\n")
    svg = re.sub(r"terminal-\d+-", "terminal-test-", svg)
    svg = re.sub(r"\b\d{2}:\d{2}:\d{2}\b", "00:00:00", svg)

    # Normalize Windows drive letters, backslashes, and HTML entities in test paths
    svg = (
        svg.replace("&#92;", "\\")
        .replace("&bsol;", "\\")
        .replace("&#x5C;", "\\")
        .replace("&#x5c;", "\\")
        .replace("&#47;", "/")
        .replace("&sol;", "/")
        .replace("&#x2F;", "/")
        .replace("&#x2f;", "/")
    )
    svg = re.sub(r"(?:[A-Za-z]:)?[/\\]+dummy", "/dummy", svg)
    svg = re.sub(
        r"/dummy([^<\"]*)",
        lambda m: "/dummy" + m.group(1).replace("\\", "/"),
        svg,
    )
    svg = re.sub(r"\b[A-Za-z]:[/\\]", "/", svg)
    svg = svg.replace("\\", "/")

    style_match = re.search(r"<style>(.*?)</style>", svg, re.DOTALL)
    if style_match:
        css_text = style_match.group(1)
        rules = re.findall(r"\.(terminal-test-r\d+)\s*\{(.*?)\}", css_text)
        if rules:
            body_without_style = svg.replace(style_match.group(0), "")
            used_rules = [
                (cls, body)
                for cls, body in rules
                if re.search(r"\b" + re.escape(cls) + r"\b", body_without_style)
            ]

            style_map = {}
            sorted_unique_styles = sorted(
                list(set(body.strip() for cls, body in used_rules))
            )

            for old_class, style_body in used_rules:
                style_index = sorted_unique_styles.index(style_body.strip())
                style_map[old_class] = f"terminal-test-c{style_index}"

            new_css_lines = [
                f"    .terminal-test-c{idx} {{ {body} }}"
                for idx, body in enumerate(sorted_unique_styles)
            ]
            new_css = "\n" + "\n".join(new_css_lines) + "\n    "
            svg = svg.replace(css_text, new_css)

            for old_class, new_class in sorted(
                style_map.items(), key=lambda x: len(x[0]), reverse=True
            ):
                svg = re.sub(r"\b" + old_class + r"\b", new_class, svg)

    return svg


def assert_svg_snapshot(snapshot_name: str, actual_svg: str) -> None:
    """Assert actual rendered SVG string matches stored reference snapshot file."""
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    snapshot_path = os.path.join(SNAPSHOT_DIR, f"{snapshot_name}.svg")
    sanitized_actual = sanitize_svg(actual_svg)

    update_snapshots = os.environ.get("UPDATE_SNAPSHOTS") == "1"

    if not os.path.exists(snapshot_path) or update_snapshots:
        with open(snapshot_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(sanitized_actual)
        if not update_snapshots:
            pytest.fail(
                f"SVG snapshot '{snapshot_name}' generated for the first time at {snapshot_path}. Run again to verify."
            )
        return

    with open(snapshot_path, "r", encoding="utf-8", newline="\n") as f:
        expected_svg = f.read().replace("\r\n", "\n")

    if sanitized_actual != expected_svg:
        with open(snapshot_path + ".actual", "w", encoding="utf-8", newline="\n") as f:
            f.write(sanitized_actual)

    assert sanitized_actual == expected_svg, (
        f"SVG visual snapshot mismatch for '{snapshot_name}'. Set UPDATE_SNAPSHOTS=1 to re-baseline."
    )


@pytest.fixture(autouse=True)
def isolated_app_dir(monkeypatch, tmp_path):
    """Ensure AppSettings is isolated from persistent disk configuration changes."""
    import app.config
    import app.core.session

    app.config.AppSettings.clear_observers()
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
    monkeypatch.setattr(
        app.core.session,
        "scan_abandoned_sessions_async",
        AsyncMock(return_value=[]),
    )
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
            for _ in range(5):
                await pilot.pause()
            svg = app.export_screenshot()
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
            for _ in range(5):
                await pilot.pause()
            svg = app.export_screenshot()
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
            for _ in range(5):
                await pilot.pause()
            svg = app.export_screenshot()
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
            for _ in range(5):
                await pilot.pause()
            # scroll any scrollable container inside the modal to the top
            for w in modal.query("*"):
                if getattr(w, "allow_vertical_scroll", False):
                    w.scroll_home(animate=False)
            modal.scroll_home(animate=False)
            await pilot.pause()
            await pilot.wait_for_scheduled_animations()
            await pilot.pause()
            svg = app.export_screenshot()
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
            for _ in range(5):
                await pilot.pause()
            svg = app.export_screenshot()
            assert_svg_snapshot("rename_modal", svg)

    asyncio.run(_test())


def test_cro_forensic_modal_snapshot():
    """Verify visual layout of CROForensicModal screen."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir="/dummy/study_root")
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(CROForensicModal(app.settings, base_dir=app.base_dir))
            for _ in range(5):
                await pilot.pause()
            svg = app.export_screenshot()
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
            for _ in range(5):
                await pilot.pause()
            svg = app.export_screenshot()
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
            for _ in range(5):
                await pilot.pause()
            svg = app.export_screenshot()
            assert_svg_snapshot("directory_select_modal", svg)

    asyncio.run(_test())
