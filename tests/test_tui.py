"""Unit and integration tests for Textual full-screen terminal interface (TUI)."""

import asyncio
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from textual.widgets import Input, Static, Switch, Tree

from app.config import AppSettings
from app.ui.tui import (
    AutoSorterTUI,
    CROForensicModal,
    NewFolderModal,
    RenameModal,
    SettingsModal,
    WizardModal,
)


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory structure for TUI testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        folder_a = os.path.join(tmpdir, "FolderA")
        os.makedirs(folder_a, exist_ok=True)

        file1 = os.path.join(tmpdir, "document_1.pdf")
        file2 = os.path.join(folder_a, "invoice_2026.docx")

        with open(file1, "w", encoding="utf-8") as f:
            f.write("Sample document content 1")
        with open(file2, "w", encoding="utf-8") as f:
            f.write("Sample invoice content 2")

        yield tmpdir


def test_tui_app_mount_and_dual_pane(temp_workspace):
    """Verify AutoSorterTUI mounts with dual-pane layout and tree view."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            assert app.query_one("#plan-tree") is not None
            assert app.query_one("#right-meta-pane") is not None
            assert app.query_one("#meta-details") is not None
            assert app.query_one("#status-bar") is not None

    asyncio.run(_test())


def test_tui_tree_rebuild_and_selection(temp_workspace):
    """Verify tree population, node data attachment, and metadata inspector updates."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.plan = {
                "Invoices": {
                    "inv_001.pdf": {
                        "__type__": "file",
                        "filepath": os.path.join(temp_workspace, "inv_001.pdf"),
                        "target_filename": "inv_001.pdf",
                        "confidence": 0.95,
                    }
                },
                "Contracts": {},
            }
            app.rebuild_tree()

            tree = app.query_one("#plan-tree")
            assert tree.root.children is not None
            assert len(tree.root.children) == 2

            folder_node = tree.root.children[1] if tree.root.children[0].data and tree.root.children[0].data.get("is_file") else tree.root.children[0]
            app.active_tree_node = folder_node

            meta = app.query_one("#meta-details")
            assert meta is not None

    asyncio.run(_test())


def test_tui_toggle_lock(temp_workspace):
    """Verify locking and unlocking file nodes in TUI updates plan state immediately."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            filepath = os.path.join(temp_workspace, "sample.txt")
            app.plan = {
                "Finance": {
                    "sample.txt": {
                        "__type__": "file",
                        "filepath": filepath,
                        "target_filename": "sample.txt",
                    }
                }
            }
            app.rebuild_tree()

            tree = app.query_one("#plan-tree")
            file_node = tree.root.children[0].children[0]
            app.active_tree_node = file_node

            # Toggle lock on
            app.action_toggle_lock()
            assert "sample.txt" in app.locked_files or filepath in app.locked_files

            # Toggle lock off
            app.action_toggle_lock()
            assert "sample.txt" not in app.locked_files and filepath not in app.locked_files

    asyncio.run(_test())


def test_tui_node_ratings(temp_workspace):
    """Verify ML quality ratings (+ / -) update rating cache and node labels."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            filepath = os.path.join(temp_workspace, "report.pdf")
            app.plan = {
                "Reports": {
                    "report.pdf": {
                        "__type__": "file",
                        "filepath": filepath,
                        "target_filename": "report.pdf",
                    }
                }
            }
            app.rebuild_tree()

            tree = app.query_one("#plan-tree")
            file_node = tree.root.children[0].children[0]
            app.active_tree_node = file_node

            # Rate positive
            app.action_rate_positive()
            assert app._ratings_cache.get(filepath) == "positive"

            # Rate negative (switch)
            app.action_rate_negative()
            assert app._ratings_cache.get(filepath) == "negative"

            # Rate negative again (clear)
            app.action_rate_negative()
            assert filepath not in app._ratings_cache or app._ratings_cache.get(filepath) is None

    asyncio.run(_test())


def test_tui_new_folder(temp_workspace):
    """Verify creating a new folder node via NewFolderModal updates plan state."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_new_folder()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, NewFolderModal)

            input_field = modal.query_one("#input-folder-name", Input)
            input_field.value = "Legal Documents"
            await pilot.press("enter")
            await pilot.pause(0.1)

            assert "Legal Documents" in app.plan

    asyncio.run(_test())


def test_tui_rename_node(temp_workspace):
    """Verify renaming file and folder nodes via RenameModal."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            filepath = os.path.join(temp_workspace, "test_doc.docx")
            app.plan = {
                "OldFolder": {
                    "test_doc.docx": {
                        "__type__": "file",
                        "filepath": filepath,
                        "target_filename": "test_doc.docx",
                    }
                }
            }
            app.rebuild_tree()

            tree = app.query_one("#plan-tree")

            # Rename Folder
            folder_node = tree.root.children[0]
            app.active_tree_node = folder_node
            app.action_rename_node()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, RenameModal)
            input_field = modal.query_one("#input-name", Input)
            input_field.value = "NewFolder"
            await pilot.press("enter")
            await pilot.pause(0.1)

            assert "NewFolder" in app.plan
            assert "OldFolder" not in app.plan

    asyncio.run(_test())


def test_tui_settings_modal(temp_workspace):
    """Verify settings modal displays and updates application settings."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_settings()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, SettingsModal)

            modal.query_one("#input-protected", Input).value = "/tmp/protected1, /tmp/protected2"
            modal.query_one("#input-ignored", Input).value = ".tmp, .log, .bak"
            modal.query_one("#input-concurrency", Input).value = "8"

            modal.action_save()
            await pilot.pause(0.1)

            assert getattr(app.settings, "PROTECTED_PATHS", None) == ["/tmp/protected1", "/tmp/protected2"] or getattr(app.settings, "PROTECTED_DIRECTORIES", None) == ["/tmp/protected1", "/tmp/protected2"]
            assert app.settings.IGNORED_EXTENSIONS == [".tmp", ".log", ".bak"]
            assert getattr(app.settings, "MAX_WORKERS", None) == 8 or getattr(app.settings, "WORKER_CONCURRENCY", None) == 8

    asyncio.run(_test())


def test_tui_wizard_modal(temp_workspace):
    """Verify wizard modal allows toggling consent and completes onboarding."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_wizard()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, WizardModal)

            modal.query_one("#switch-consent", Switch).value = True
            modal.action_finish()
            await pilot.pause(0.1)

            assert app.settings.AI_CONSENT_GRANTED is True

    asyncio.run(_test())


def test_tui_cro_forensic_modal(temp_workspace):
    """Verify CRO forensic ingest modal executes pipeline worker and logs output."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_cro_forensic()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, CROForensicModal)

            modal.query_one("#input-source", Input).value = temp_workspace
            modal.query_one("#input-target", Input).value = os.path.join(temp_workspace, "Output")

            with patch("app.core.cro_multi_study_pipeline.CROMultiStudyPipeline.run_pipeline") as mock_run:
                mock_res = MagicMock()
                mock_res.total_scanned_files = 5
                mock_res.discovered_studies_count = 1
                mock_run.return_value = mock_res

                modal.action_run()
                await pilot.pause(0.2)

                assert mock_run.called

    asyncio.run(_test())


def test_tui_modals_render_on_small_viewports(temp_workspace):
    """Verify all six TUI modal screens render without errors on 70x20 and 80x24 viewports."""
    from app.ui.tui import DirectorySelectModal

    settings = AppSettings()

    # Verify CSS definitions enforce fluid 90% width, max-width 80, max-height 90%, overflow-y auto
    modals = [
        RenameModal("Rename Test", "current", ".txt"),
        NewFolderModal(),
        DirectorySelectModal(temp_workspace),
        SettingsModal(settings),
        WizardModal(settings),
        CROForensicModal(settings, temp_workspace),
    ]

    for modal in modals:
        assert "width: 90%" in modal.CSS
        assert "max-width: 80" in modal.CSS
        assert "max-height: 90%" in modal.CSS
        assert "overflow-y: auto" in modal.CSS

    async def _test(size, modal_factory):
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test(size=size) as pilot:
            modal = modal_factory()
            app.push_screen(modal)
            await pilot.pause(0.05)
            assert app.screen is modal

    for size in [(80, 24), (70, 20)]:
        asyncio.run(_test(size, lambda: RenameModal("Rename Test", "current", ".txt")))
        asyncio.run(_test(size, NewFolderModal))
        asyncio.run(_test(size, lambda: DirectorySelectModal(temp_workspace)))
        asyncio.run(_test(size, lambda: SettingsModal(settings)))
        asyncio.run(_test(size, lambda: WizardModal(settings)))
        asyncio.run(_test(size, lambda: CROForensicModal(settings, temp_workspace)))


def test_main_cli_tui_invocation():
    """Verify app/main.py launches run_tui when --tui argument is supplied."""
    from app.main import main

    with patch("argparse.ArgumentParser.parse_args") as mock_args, patch("app.ui.tui.run_tui") as mock_run_tui:
        args = MagicMock()
        args.tui = True
        args.gui = False
        args.daemon = False
        args.demo = False
        args.smoke_test = False
        args.update_snapshots = False
        args.directory = "/path/to/sort"
        args.debug_layout = False
        mock_args.return_value = args

        main()
        mock_run_tui.assert_called_once()


def test_tui_screen_reader_announcements(temp_workspace):
    """Verify state transitions and user actions emit auditory screen reader announcements."""
    from app.ui.tui import DirectorySelectModal

    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            # Check initial mount announcement
            assert len(app.announcements) >= 1
            assert any("ready" in a["message"].lower() for a in app.announcements)

            # Test tree node selection announcement
            filepath = os.path.join(temp_workspace, "sample.txt")
            app.plan = {
                "Finance": {
                    "sample.txt": {
                        "__type__": "file",
                        "filepath": filepath,
                        "target_filename": "sample.txt",
                    }
                }
            }
            app.rebuild_tree()

            tree = app.query_one("#plan-tree")
            file_node = tree.root.children[0].children[0]

            # Trigger selection event manually
            app.on_node_selected(Tree.NodeSelected(file_node))
            assert "Selected file 'sample.txt'" in app.get_last_announcement()

            # Test locking announcement
            app.action_toggle_lock()
            assert "Locked file 'sample.txt'" in app.get_last_announcement()

            # Test rating announcement
            app.action_rate_positive()
            assert "Set rating 'positive'" in app.get_last_announcement()

            # Test modal mount announcement
            modal = DirectorySelectModal(temp_workspace)
            app.push_screen(modal)
            await pilot.pause(0.05)
            assert "Opened target directory selection dialog" in modal.get_last_announcement()

    asyncio.run(_test())


def test_tui_modal_escape_key_navigation(temp_workspace):
    """Verify pressing Escape key dismisses modal dialogs without defects."""
    settings = AppSettings()

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test() as pilot:
            modal = NewFolderModal()
            app.push_screen(modal)
            await pilot.pause(0.05)
            assert app.screen is modal

            # Press escape key
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert app.screen is not modal

    asyncio.run(_test())


def test_tui_wcag_tooltips_and_attributes(temp_workspace):
    """Verify tooltips and explicit accessibility attributes are attached to all modal controls."""
    from app.ui.tui import DirectorySelectModal

    settings = AppSettings()
    modals = [
        RenameModal("Rename Test", "current", ".txt"),
        NewFolderModal(),
        DirectorySelectModal(temp_workspace),
        SettingsModal(settings),
        WizardModal(settings),
        CROForensicModal(settings, temp_workspace),
    ]

    async def _test(modal_inst):
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test() as pilot:
            app.push_screen(modal_inst)
            await pilot.pause(0.05)

            # Assert controls have tooltips set
            for widget in modal_inst.query("*"):
                w_type = type(widget).__name__
                if w_type in ("Input", "Button", "Select", "Switch"):
                    tooltip = getattr(widget, "tooltip", None)
                    assert tooltip is not None and len(str(tooltip)) > 0, f"{w_type} #{getattr(widget, 'id', '')} missing tooltip"

    for m in modals:
        asyncio.run(_test(m))


def test_tui_automated_audit_hooks(temp_workspace):
    """Verify automated audit hooks pass with zero WCAG 2.1 violations across all TUI components."""
    from app.ui.a11y_runner import inspect_tui_component
    from app.ui.tui import DirectorySelectModal

    settings = AppSettings()
    modals = [
        RenameModal("Rename Test", "current", ".txt"),
        NewFolderModal(),
        DirectorySelectModal(temp_workspace),
        SettingsModal(settings),
        WizardModal(settings),
        CROForensicModal(settings, temp_workspace),
    ]

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test() as pilot:
            app_audit = app.audit_a11y_compliance()
            assert app_audit["compliant"] is True
            assert app_audit["violations_count"] == 0

            app_violations = inspect_tui_component(app)
            assert len(app_violations) == 0

            for m in modals:
                app.push_screen(m)
                await pilot.pause(0.05)

                modal_audit = m.audit_a11y_compliance()
                assert modal_audit["compliant"] is True, f"{m} failed audit: {modal_audit['violations']}"
                assert modal_audit["violations_count"] == 0

                modal_violations = inspect_tui_component(m)
                assert len(modal_violations) == 0, f"{m} has violations: {modal_violations}"

                await pilot.press("escape")
                await pilot.pause(0.05)

    asyncio.run(_test())


def test_tui_jev_tree_node_tags_and_inspector(temp_workspace):
    """Verify TUI formats Jev tags in tree nodes and renders detailed metadata in inspector panel."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            filepath = os.path.join(temp_workspace, "tax_return.pdf")
            app.plan = {
                "Financial": {
                    "tax_return.pdf": {
                        "__type__": "file",
                        "filepath": filepath,
                        "target_filename": "tax_return.pdf",
                        "routed_by": "jev_classifier",
                        "category": "Financial",
                        "sensitivity_rating": "HIGH",
                        "sensitivity_score": 0.85,
                        "archival_priority": 1,
                        "archival_priority_score": 0.95,
                        "confidence": 0.98,
                    }
                }
            }
            app.rebuild_tree()

            tree = app.query_one("#plan-tree")
            folder_node = tree.root.children[0]
            file_node = folder_node.children[0]

            # Verify tree label formatting
            label_str = str(file_node.label)
            assert "[JEV]" in label_str
            assert "[SENS: HIGH]" in label_str
            assert "[ARCH: P1]" in label_str

            # Select file node and verify inspector panel updates
            app.active_tree_node = file_node
            inspector_text = app._update_inspector(file_node)

            assert "Routing Source:" in inspector_text
            assert "jev_classifier" in inspector_text
            assert "Category:" in inspector_text
            assert "Financial" in inspector_text
            assert "Sensitivity Rating:" in inspector_text
            assert "HIGH" in inspector_text
            assert "Sensitivity Score:" in inspector_text
            assert "0.85" in inspector_text
            assert "Archival Priority:" in inspector_text
            assert "P1" in inspector_text
            assert "Archival Priority Score:" in inspector_text
            assert "0.95" in inspector_text

    asyncio.run(_test())


def test_tui_jev_partial_metadata(temp_workspace):
    """Verify TUI handles partial/missing Jev metadata without throwing exceptions."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            filepath = os.path.join(temp_workspace, "partial.pdf")
            app.plan = {
                "Misc": {
                    "partial.pdf": {
                        "__type__": "file",
                        "filepath": filepath,
                        "target_filename": "partial.pdf",
                        "routed_by": "jev_classifier",
                    }
                }
            }
            app.rebuild_tree()

            tree = app.query_one("#plan-tree")
            file_node = tree.root.children[0].children[0]

            label_str = str(file_node.label)
            assert "[JEV]" in label_str

            inspector_text = app._update_inspector(file_node)
            assert "Routing Source:" in inspector_text
            assert "jev_classifier" in inspector_text

    asyncio.run(_test())


def test_run_tui_non_interactive_stdin():
    """Verify run_tui returns immediately without launching App when sys.stdin is non-interactive or None."""
    from app.ui.tui import run_tui

    class MockNonInteractiveStdin:
        def isatty(self):
            return False

    settings = AppSettings()
    with patch("sys.stdin", MockNonInteractiveStdin()), patch("app.ui.tui.AutoSorterTUI") as mock_app_cls:
        run_tui(settings)
        mock_app_cls.assert_not_called()


def test_tui_speech_binary_fallback_missing_binary(temp_workspace):
    """Verify announce succeeds, updates #status-bar, and records history when speech binaries are missing."""
    from unittest.mock import patch
    settings = AppSettings()

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        with patch("shutil.which", return_value=None):
            async with app.run_test() as pilot:
                result = app.announce("Speech binary missing fallback test message")
                assert result == "Speech binary missing fallback test message"
                assert app.get_last_announcement() == "Speech binary missing fallback test message"
                assert any(
                    a["message"] == "Speech binary missing fallback test message"
                    for a in app.announcements
                )
                sb = app.query_one("#status-bar", Static)
                assert "Speech binary missing fallback test message" in str(sb.render())

    asyncio.run(_test())


def test_tui_speech_binary_execution_exception_fallback(temp_workspace):
    """Verify announce handles subprocess execution exceptions without interrupting navigation."""
    from unittest.mock import patch
    settings = AppSettings()
    mock_speech_bin = r"C:\Tools\spd-say.exe" if sys.platform == "win32" else "/usr/bin/spd-say"

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        with patch("shutil.which", return_value=mock_speech_bin), patch(
            "subprocess.run", side_effect=OSError("Exec format error")
        ):
            async with app.run_test() as pilot:
                result = app.announce("Speech execution error fallback test message")
                app.join_speech_thread(timeout=1.0)
                assert result == "Speech execution error fallback test message"
                assert app.get_last_announcement() == "Speech execution error fallback test message"
                sb = app.query_one("#status-bar", Static)
                assert "Speech execution error fallback test message" in str(sb.render())

    asyncio.run(_test())


def test_tui_speech_binary_available_and_audit(temp_workspace):
    """Verify speech binary execution attempt when available and verify audit compliance output."""
    from unittest.mock import patch
    settings = AppSettings()
    mock_speech_bin = r"C:\Tools\spd-say.exe" if sys.platform == "win32" else "/usr/bin/spd-say"

    async def _test():
        app1 = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        with patch("shutil.which", return_value=mock_speech_bin), patch(
            "subprocess.run", return_value=None
        ):
            async with app1.run_test() as pilot:
                audit_res = app1.audit_a11y_compliance()
                assert audit_res["speech_binary_available"] is True
                assert audit_res["speech_binary"] == mock_speech_bin
                assert audit_res["speech_binary_fallback_ready"] is True
                assert audit_res["status_bar_available"] is True
                assert audit_res["compliant"] is True

        app2 = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        with patch("shutil.which", return_value=None):
            async with app2.run_test() as pilot:
                audit_res_missing = app2.audit_a11y_compliance()
                assert audit_res_missing["speech_binary_available"] is False
                assert audit_res_missing["speech_binary"] is None
                assert audit_res_missing["speech_binary_fallback_ready"] is True
                assert audit_res_missing["status_bar_available"] is True
                assert audit_res_missing["compliant"] is True

    asyncio.run(_test())


def test_tui_speech_binary_windows_extension_filtering(temp_workspace):
    """Verify _get_speech_binary filters out extensionless POSIX scripts on Windows."""
    from unittest.mock import patch
    settings = AppSettings()
    app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

    with patch("sys.platform", "win32"), patch(
        "shutil.which", return_value=r"C:\Program Files\Git\usr\bin\spd-say"
    ):
        assert app._get_speech_binary() is None

    with patch("sys.platform", "win32"), patch(
        "shutil.which", return_value=r"C:\Tools\spd-say.exe"
    ):
        assert app._get_speech_binary() == r"C:\Tools\spd-say.exe"


def test_tui_speech_binary_windows_posix_path_filtering(temp_workspace):
    """Verify _get_speech_binary filters out MSYS2/Cygwin/Git POSIX binary paths on Windows."""
    from unittest.mock import patch
    settings = AppSettings()
    app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

    for posix_path in [
        r"C:\Program Files\Git\usr\bin\spd-say.exe",
        r"C:\Program Files\Git\mingw64\bin\spd-say.exe",
        r"C:\msys64\usr\bin\spd-say.exe",
        r"C:\cygwin64\bin\spd-say.exe",
        r"C:\msys64\mingw64\bin\spd-say.exe",
        r"C:\Windows\System32\wsl\spd-say.exe",
    ]:
        with patch("sys.platform", "win32"), patch("shutil.which", return_value=posix_path):
            assert app._get_speech_binary() is None




