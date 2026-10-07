"""Unit and integration tests for Textual full-screen terminal interface (TUI)."""

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from textual.widgets import Button, Input, Static, Switch, Tree

from app.config import AppSettings
from app.ui.tui import (
    AutoSorterTUI,
    CROForensicModal,
    DropZoneModal,
    NewFolderModal,
    RenameModal,
    SessionRecoveryModal,
    SettingsModal,
    ShortcutCheatSheetModal,
    WizardModal,
)

pytestmark = pytest.mark.xdist_group(name="tui")


@pytest.fixture(autouse=True)
def isolated_app_dir(monkeypatch, tmp_path):
    """Ensure AppSettings is isolated from persistent disk configuration changes."""
    import app.config
    import app.core.session

    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path))
    monkeypatch.setattr(app.config, "get_app_dir", lambda: tmp_path)
    monkeypatch.setattr(
        app.config.AppSettings, "_trigger_save", lambda self: self._save()
    )
    monkeypatch.setattr(
        app.core.session,
        "scan_abandoned_sessions_async",
        AsyncMock(return_value=[]),
    )
    monkeypatch.delenv("AUTOSORTER_PROTECTED_PATHS", raising=False)
    monkeypatch.delenv("AUTOSORTER_IGNORED_EXTENSIONS", raising=False)


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
        settings._settings_model.AI_CONSENT_GRANTED = True
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
        settings._settings_model.AI_CONSENT_GRANTED = True
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

            folder_node = (
                tree.root.children[1]
                if tree.root.children[0].data
                and tree.root.children[0].data.get("is_file")
                else tree.root.children[0]
            )
            app.active_tree_node = folder_node

            meta = app.query_one("#meta-details")
            assert meta is not None

    asyncio.run(_test())


def test_tui_toggle_lock(temp_workspace):
    """Verify locking and unlocking file nodes in TUI updates plan state immediately."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
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
            assert (
                "sample.txt" not in app.locked_files
                and filepath not in app.locked_files
            )

    asyncio.run(_test())


def test_tui_node_ratings(temp_workspace):
    """Verify ML quality ratings (+ / -) update rating cache and node labels."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
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
            assert (
                filepath not in app._ratings_cache
                or app._ratings_cache.get(filepath) is None
            )

    asyncio.run(_test())


def test_tui_new_folder(temp_workspace):
    """Verify creating a new folder node via NewFolderModal updates plan state."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
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
        settings._settings_model.AI_CONSENT_GRANTED = True
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
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        with patch.object(AppSettings, "_save", return_value=None):
            async with app.run_test() as pilot:
                app.action_open_settings()
                await pilot.pause(0.1)

                modal = app.screen
                assert isinstance(modal, SettingsModal)

                modal.query_one(
                    "#input-protected", Input
                ).value = "/tmp/protected1, /tmp/protected2"
                modal.query_one("#input-ignored", Input).value = ".tmp, .log, .bak"
                modal.query_one("#input-concurrency", Input).value = "8"

                modal.action_save()
                await pilot.pause(0.1)

                assert getattr(app.settings, "PROTECTED_PATHS", None) == [
                    "/tmp/protected1",
                    "/tmp/protected2",
                ] or getattr(app.settings, "PROTECTED_DIRECTORIES", None) == [
                    "/tmp/protected1",
                    "/tmp/protected2",
                ]
                assert app.settings.IGNORED_EXTENSIONS == [".tmp", ".log", ".bak"]
                assert (
                    getattr(app.settings, "MAX_WORKERS", None) == 8
                    or getattr(app.settings, "WORKER_CONCURRENCY", None) == 8
                )

    asyncio.run(_test())


def test_tui_first_run_auto_triggers_wizard(temp_workspace):
    """Verify first-run launch with unconfigured AI consent automatically triggers WizardModal after layout refresh."""

    async def _test():
        settings = AppSettings()
        assert settings.AI_CONSENT_GRANTED is None
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            assert isinstance(app.screen, WizardModal)

    asyncio.run(_test())


def test_tui_returning_launch_bypasses_wizard(temp_workspace):
    """Verify returning launch with explicit AI consent settings bypasses WizardModal."""

    async def _test():
        # Case 1: Consent granted = True
        settings_true = AppSettings()
        settings_true.AI_CONSENT_GRANTED = True
        app_true = AutoSorterTUI(settings=settings_true, base_dir=temp_workspace)

        async with app_true.run_test() as pilot:
            await pilot.pause(0.1)
            assert not isinstance(app_true.screen, WizardModal)

        # Case 2: Consent granted = False
        settings_false = AppSettings()
        settings_false.AI_CONSENT_GRANTED = False
        app_false = AutoSorterTUI(settings=settings_false, base_dir=temp_workspace)

        async with app_false.run_test() as pilot:
            await pilot.pause(0.1)
            assert not isinstance(app_false.screen, WizardModal)

    asyncio.run(_test())


def test_tui_wizard_modal_cancel_preserves_defaults(temp_workspace):
    """Verify canceling WizardModal maintains defaults and emits announcement."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = None
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            modal = app.screen
            assert isinstance(modal, WizardModal)

            modal.action_cancel()
            await pilot.pause(0.1)

            assert app.settings.AI_CONSENT_GRANTED is False
            assert not isinstance(app.screen, WizardModal)

    asyncio.run(_test())


def test_tui_wizard_modal_finish_persists_settings(temp_workspace):
    """Verify wizard modal allows toggling consent, persists settings, and completes onboarding."""

    async def _test():
        settings = AppSettings(filepath=os.path.join(temp_workspace, "settings.json"))
        settings._settings_model.AI_CONSENT_GRANTED = None
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            for _ in range(5):
                await pilot.pause()
            modal = app.screen
            assert isinstance(modal, WizardModal)

            modal.query_one("#switch-consent", Switch).value = False
            with patch.object(AppSettings, "_save") as mock_save:
                modal.action_finish()
                for _ in range(5):
                    await pilot.pause()

                assert app.settings.AI_CONSENT_GRANTED is False
                assert mock_save.called
                assert not isinstance(app.screen, WizardModal)

    asyncio.run(_test())


def test_tui_cro_forensic_modal(temp_workspace):
    """Verify CRO forensic ingest modal executes pipeline worker and logs output."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_cro_forensic()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, CROForensicModal)

            modal.query_one("#input-source", Input).value = temp_workspace
            modal.query_one("#input-target", Input).value = os.path.join(
                temp_workspace, "Output"
            )

            with patch(
                "app.plugins.clinical_compliance.cro_multi_study_pipeline.CROMultiStudyPipeline.run_pipeline"
            ) as mock_run:
                mock_res = MagicMock()
                mock_res.total_scanned_files = 5
                mock_res.discovered_studies_count = 1
                mock_run.return_value = mock_res

                modal.action_run()
                await pilot.pause(0.2)

                assert mock_run.called

    asyncio.run(_test())


def test_tui_modals_render_on_small_viewports(temp_workspace):
    """Verify all TUI modal screens render without errors on 70x20 and 80x24 viewports."""
    from app.ui.tui import DirectorySelectModal

    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    modals = [
        RenameModal("Rename Test", "current", ".txt"),
        NewFolderModal(),
        DirectorySelectModal(temp_workspace),
        SettingsModal(settings),
        WizardModal(settings),
        CROForensicModal(settings, temp_workspace),
        SessionRecoveryModal(
            {"session_id": "s1", "base_dir": temp_workspace, "status": "failed"}
        ),
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
        asyncio.run(
            _test(
                size,
                lambda: SessionRecoveryModal(
                    {"session_id": "s1", "base_dir": temp_workspace, "status": "failed"}
                ),
            )
        )
        asyncio.run(_test(size, ShortcutCheatSheetModal))


def test_main_cli_tui_invocation():
    """Verify app/main.py launches run_tui when --tui argument is supplied."""
    from app.main import main

    with (
        patch("argparse.ArgumentParser.parse_args") as mock_args,
        patch("app.ui.tui.run_tui") as mock_run_tui,
    ):
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
        settings._settings_model.AI_CONSENT_GRANTED = True
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
            assert (
                "Opened target directory selection dialog"
                in modal.get_last_announcement()
            )

    asyncio.run(_test())


def test_tui_modal_escape_key_navigation(temp_workspace):
    """Verify pressing Escape key dismisses modal dialogs without defects."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

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
    settings._settings_model.AI_CONSENT_GRANTED = True
    modals = [
        RenameModal("Rename Test", "current", ".txt"),
        NewFolderModal(),
        DirectorySelectModal(temp_workspace),
        SettingsModal(settings),
        WizardModal(settings),
        CROForensicModal(settings, temp_workspace),
        SessionRecoveryModal(
            {"session_id": "s1", "base_dir": temp_workspace, "status": "failed"}
        ),
        ShortcutCheatSheetModal(),
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
                    assert tooltip is not None and len(str(tooltip)) > 0, (
                        f"{w_type} #{getattr(widget, 'id', '')} missing tooltip"
                    )

    for m in modals:
        asyncio.run(_test(m))


def test_tui_automated_audit_hooks(temp_workspace):
    """Verify automated audit hooks pass with zero WCAG 2.1 violations across all TUI components."""
    from app.ui.tui import DirectorySelectModal, inspect_tui_component

    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    modals = [
        RenameModal("Rename Test", "current", ".txt"),
        NewFolderModal(),
        DirectorySelectModal(temp_workspace),
        SettingsModal(settings),
        WizardModal(settings),
        CROForensicModal(settings, temp_workspace),
        SessionRecoveryModal(
            {"session_id": "s1", "base_dir": temp_workspace, "status": "failed"}
        ),
        ShortcutCheatSheetModal(),
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
                assert modal_audit["compliant"] is True, (
                    f"{m} failed audit: {modal_audit['violations']}"
                )
                assert modal_audit["violations_count"] == 0

                modal_violations = inspect_tui_component(m)
                assert len(modal_violations) == 0, (
                    f"{m} has violations: {modal_violations}"
                )

                await pilot.press("escape")
                await pilot.pause(0.05)

    asyncio.run(_test())


def test_tui_session_recovery_modal_actions(temp_workspace):
    """Verify SessionRecoveryModal options (Resume, Rollback, Clean)."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    session_info = {
        "session_id": "test_session_123",
        "base_dir": temp_workspace,
        "status": "interrupted",
    }

    async def _test_resume():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test() as pilot:
            modal = SessionRecoveryModal(session_info)
            res = None

            def on_dismiss(val):
                nonlocal res
                res = val

            app.push_screen(modal, on_dismiss)
            await pilot.pause(0.05)
            await pilot.click("#btn-resume")
            await pilot.pause(0.05)
            assert res == "resume"

    async def _test_rollback():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test() as pilot:
            modal = SessionRecoveryModal(session_info)
            res = None

            def on_dismiss(val):
                nonlocal res
                res = val

            app.push_screen(modal, on_dismiss)
            await pilot.pause(0.05)
            await pilot.click("#btn-rollback")
            await pilot.pause(0.05)
            assert res == "rollback"

    async def _test_clean():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test() as pilot:
            modal = SessionRecoveryModal(session_info)
            res = None

            def on_dismiss(val):
                nonlocal res
                res = val

            app.push_screen(modal, on_dismiss)
            await pilot.pause(0.05)
            await pilot.click("#btn-clean")
            await pilot.pause(0.05)
            assert res == "clean"

    asyncio.run(_test_resume())
    asyncio.run(_test_rollback())
    asyncio.run(_test_clean())


def test_tui_jev_tree_node_tags_and_inspector(temp_workspace):
    """Verify TUI formats Jev tags in tree nodes and renders detailed metadata in inspector panel."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
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


def test_tui_expanded_settings_fields(temp_workspace):
    """Verify SettingsModal MAX_FOLDERS, CLINICAL_SMART_RENAMING, CONTEXTUAL_RENAMING, and AI_CONSENT_GRANTED controls."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        with patch.object(AppSettings, "_save", return_value=None):
            async with app.run_test() as pilot:
                app.action_open_settings()
                await pilot.pause(0.1)

                modal = app.screen
                assert isinstance(modal, SettingsModal)

                modal.query_one("#input-max-folders", Input).value = "8"
                modal.query_one("#switch-clinical-renaming", Switch).value = True
                modal.query_one("#switch-contextual-renaming", Switch).value = True
                modal.query_one("#switch-ai-consent", Switch).value = False

                modal.action_save()
                await pilot.pause(0.1)

                assert app.settings.MAX_FOLDERS == 8
                assert app.settings.CLINICAL_SMART_RENAMING is True
                assert app.settings.CONTEXTUAL_RENAMING is True
                assert app.settings.AI_CONSENT_GRANTED is False
                sb = app.query_one("#status-bar", Static)
                assert "[AI: Disabled]" in str(sb.render())

    asyncio.run(_test())


def test_tui_settings_modal_ai_consent_toggle_flow(temp_workspace):
    """Verify toggling AI consent inside SettingsModal updates settings and status bar badge dynamically."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = False
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace, skip_wizard=True)

        with patch.object(AppSettings, "_save") as mock_save:
            async with app.run_test() as pilot:
                sb = app.query_one("#status-bar", Static)
                assert "[AI: Disabled]" in str(sb.render())

                # Open settings modal and enable AI consent
                app.action_open_settings()
                await pilot.pause(0.1)
                modal = app.screen
                assert isinstance(modal, SettingsModal)
                sw = modal.query_one("#switch-ai-consent", Switch)
                assert sw.value is False
                sw.value = True

                modal.action_save()
                await pilot.pause(0.1)

                assert app.settings.AI_CONSENT_GRANTED is True
                assert mock_save.called
                assert "[AI: Active]" in str(sb.render())

                # Re-open settings modal and disable AI consent
                app.action_open_settings()
                await pilot.pause(0.1)
                modal2 = app.screen
                assert isinstance(modal2, SettingsModal)
                sw2 = modal2.query_one("#switch-ai-consent", Switch)
                assert sw2.value is True
                sw2.value = False

                modal2.action_save()
                await pilot.pause(0.1)

                assert app.settings.AI_CONSENT_GRANTED is False
                assert "[AI: Disabled]" in str(sb.render())

    asyncio.run(_test())


def test_tui_jev_partial_metadata(temp_workspace):
    """Verify TUI handles partial/missing Jev metadata without throwing exceptions."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
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


def test_main_cli_interactive_subcommand_flags():
    """Verify smart-autosorter sort <dir> --interactive and --tui launch run_tui."""
    from app.main import main

    for flag in ["--tui", "--interactive"]:
        with (
            patch("sys.argv", ["smart-autosorter", "sort", "/tmp/testdir", flag]),
            patch("app.ui.tui.run_tui") as mock_run_tui,
        ):
            try:
                main()
            except SystemExit:
                pass
            mock_run_tui.assert_called_once()


def test_run_tui_guardrails(monkeypatch):
    """Verify non-TTY and small terminal dimension guardrails in run_tui."""
    from app.ui.tui import run_tui

    settings = AppSettings()

    monkeypatch.delenv("FORCE_TUI", raising=False)
    monkeypatch.delenv("IGNORE_TERMINAL_SIZE", raising=False)

    # Test non-TTY exit
    with (
        patch("sys.stdin.isatty", return_value=False),
        pytest.raises(SystemExit) as exc1,
    ):
        run_tui(settings)
    assert exc1.value.code == 1

    # Test small dimensions exit
    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("sys.stdout.isatty", return_value=True),
        patch("shutil.get_terminal_size", return_value=(70, 20)),
        pytest.raises(SystemExit) as exc2,
    ):
        run_tui(settings)
    assert exc2.value.code == 1


def test_tui_speech_binary_fallback_missing_binary(temp_workspace):
    """Verify announce succeeds, updates #status-bar, and records history when speech binaries are missing."""
    from unittest.mock import patch

    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        with patch("shutil.which", return_value=None):
            async with app.run_test() as pilot:
                result = app.announce("Speech binary missing fallback test message")
                assert result == "Speech binary missing fallback test message"
                assert (
                    app.get_last_announcement()
                    == "Speech binary missing fallback test message"
                )
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
    settings._settings_model.AI_CONSENT_GRANTED = True
    mock_speech_bin = "/usr/bin/spd-say"

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        with (
            patch("sys.platform", "linux"),
            patch("shutil.which", return_value=mock_speech_bin),
            patch("subprocess.run", side_effect=OSError("Exec format error")),
        ):
            async with app.run_test() as pilot:
                result = app.announce("Speech execution error fallback test message")
                app.join_speech_thread(timeout=1.0)
                assert result == "Speech execution error fallback test message"
                assert (
                    app.get_last_announcement()
                    == "Speech execution error fallback test message"
                )
                sb = app.query_one("#status-bar", Static)
                assert "Speech execution error fallback test message" in str(
                    sb.render()
                )

    asyncio.run(_test())


def test_tui_speech_binary_available_and_audit(temp_workspace):
    """Verify speech binary execution attempt when available and verify audit compliance output."""
    from unittest.mock import patch

    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    mock_speech_bin = "/usr/bin/spd-say"

    async def _test():
        app1 = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        with (
            patch("sys.platform", "linux"),
            patch("shutil.which", return_value=mock_speech_bin),
            patch("subprocess.run", return_value=None),
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
    """Verify _get_speech_binary returns None on Windows (defaulting to visual live region status bar)."""
    from unittest.mock import patch

    settings = AppSettings()
    app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

    with patch("sys.platform", "win32"):
        assert app._get_speech_binary() is None


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
        r"C:\ProgramData\chocolatey\bin\spd-say.exe",
        r"C:\actions-runner\_work\spd-say.exe",
        r"C:\runner\_work\spd-say.exe",
        r"C:\hostedtoolcache\windows\spd-say.exe",
    ]:
        with (
            patch("sys.platform", "win32"),
            patch("shutil.which", return_value=posix_path),
        ):
            assert app._get_speech_binary() is None


def test_tui_adaptive_breakpoint_layout_narrow_and_wide(temp_workspace):
    """Verify AutoSorterTUI toggles narrow container class based on 100-column breakpoint."""

    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        # Test narrow viewport (< 100 cols)
        async with app.run_test(size=(80, 24)) as pilot:
            dual_pane = app.query_one("#main-dual-pane")
            assert dual_pane.has_class("narrow")

            # Simulate resize event to wide viewport (120 cols >= 100)
            await pilot.resize_terminal(120, 30)
            await pilot.pause()
            assert not dual_pane.has_class("narrow")

            # Simulate resize back to narrow viewport (80 cols < 100)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert dual_pane.has_class("narrow")

    asyncio.run(_test())


def test_modal_screens_responsive_layout(temp_workspace):
    """Verify modal screens toggle narrow class when resized below 80 columns."""

    async def _test():
        settings = AppSettings()

        # Test RenameModal on narrow viewport
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test(size=(75, 24)) as pilot:
            modal = RenameModal("Test Item", "test.txt")
            app.push_screen(modal)
            await pilot.pause()
            assert modal.has_class("narrow")

            # Resize to wide
            await pilot.resize_terminal(100, 30)
            await pilot.pause()
            assert not modal.has_class("narrow")

        # Test SettingsModal on narrow viewport
        app2 = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app2.run_test(size=(75, 24)) as pilot:
            modal2 = SettingsModal(settings)
            app2.push_screen(modal2)
            await pilot.pause()
            assert modal2.has_class("narrow")

            await pilot.resize_terminal(100, 30)
            await pilot.pause()
            assert not modal2.has_class("narrow")

    asyncio.run(_test())


def test_tui_vim_tree_navigation(temp_workspace):
    """Verify native vim motion navigation (h, j, k, l) on Tree control."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.plan = {
                "FolderA": {
                    "file1.txt": {
                        "__type__": "file",
                        "filepath": os.path.join(temp_workspace, "file1.txt"),
                        "target_filename": "file1.txt",
                    }
                },
                "FolderB": {},
            }
            app.rebuild_tree()

            tree = app.query_one("#plan-tree")
            tree.focus()
            await pilot.pause(0.05)

            folder_a_node = tree.root.children[0]
            tree.move_cursor(folder_a_node)
            assert tree.cursor_node == folder_a_node

            # Test 'j' (down)
            await pilot.press("j")
            await pilot.pause(0.05)
            assert tree.cursor_node != folder_a_node

            # Test 'k' (up)
            await pilot.press("k")
            await pilot.pause(0.05)
            assert tree.cursor_node == folder_a_node

            # Test 'h' (collapse folder)
            assert folder_a_node.is_expanded
            await pilot.press("h")
            await pilot.pause(0.05)
            assert not folder_a_node.is_expanded

            # Test 'l' (expand folder)
            await pilot.press("l")
            await pilot.pause(0.05)
            assert folder_a_node.is_expanded

            # Test 'l' again on expanded folder (move to first child)
            await pilot.press("l")
            await pilot.pause(0.05)
            child_node = folder_a_node.children[0]
            assert tree.cursor_node == child_node

            # Test 'h' on child leaf node (move to parent folder node)
            await pilot.press("h")
            await pilot.pause(0.05)
            assert tree.cursor_node == folder_a_node

            # Test unfocused tree ignores vim keys
            tree.blur()
            await pilot.pause(0.05)
            assert not tree.has_focus

            tree.move_cursor(folder_a_node)
            await pilot.press("j")
            await pilot.pause(0.05)
            assert tree.cursor_node == folder_a_node

    asyncio.run(_test())


def test_tui_scoped_hotkeys_ctrl_combinations(temp_workspace):
    """Verify remapped Ctrl+... modifier hotkey actions."""
    from app.ui.tui import DirectorySelectModal

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            filepath = os.path.join(temp_workspace, "doc.pdf")
            app.plan = {
                "FolderA": {
                    "doc.pdf": {
                        "__type__": "file",
                        "filepath": filepath,
                        "target_filename": "doc.pdf",
                    }
                }
            }
            app.rebuild_tree()

            tree = app.query_one("#plan-tree")
            file_node = tree.root.children[0].children[0]
            app.active_tree_node = file_node

            # Ctrl+L -> Lock
            await pilot.press("ctrl+l")
            await pilot.pause(0.05)
            assert "doc.pdf" in app.locked_files or filepath in app.locked_files

            # Ctrl+N -> New Folder modal
            await pilot.press("ctrl+n")
            await pilot.pause(0.05)
            assert isinstance(app.screen, NewFolderModal)
            await pilot.press("escape")
            await pilot.pause(0.05)

            # Ctrl+O -> Settings modal
            await pilot.press("ctrl+o")
            await pilot.pause(0.05)
            assert isinstance(app.screen, SettingsModal)
            await pilot.press("escape")
            await pilot.pause(0.05)

            # Ctrl+B -> Directory Select modal
            await pilot.press("ctrl+b")
            await pilot.pause(0.05)
            assert isinstance(app.screen, DirectorySelectModal)
            await pilot.press("escape")
            await pilot.pause(0.05)

    asyncio.run(_test())


def test_tui_input_focus_guard_clauses(temp_workspace):
    """Verify input fields guard against triggering background application hotkey actions."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.plan = {"FolderA": {}}
            app.rebuild_tree()

            modal = NewFolderModal()
            app.push_screen(modal)
            await pilot.pause(0.05)

            input_widget = modal.query_one("#input-folder-name", Input)
            input_widget.focus()
            await pilot.pause(0.05)

            assert app._is_text_control_focused() is True

            # Invoking action handlers directly when text control is focused must return early
            initial_plan_keys = set(app.plan.keys())
            app.action_new_folder()
            assert app.screen is modal  # No new modal opened

            app.action_toggle_lock()
            app.action_scan_directory()
            app.action_execute_sort()

            # Fast typing inside input field does not trigger background application actions
            await pilot.press("l", "r", "n", "s", "e", "b", "q")
            await pilot.pause(0.05)

            assert input_widget.value == "lrnsebq"
            assert set(app.plan.keys()) == initial_plan_keys

    asyncio.run(_test())


def test_tui_shortcut_cheat_sheet_modal_trigger_and_dismiss(temp_workspace):
    """Verify ShortcutCheatSheetModal triggers via ?, F1, or action, and dismisses via Escape, ?, F1, or Close button."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)

            # Trigger via action_open_cheat_sheet
            app.action_open_cheat_sheet()
            await pilot.pause(0.05)
            modal = app.screen
            assert isinstance(modal, ShortcutCheatSheetModal)
            assert (
                "Opened keyboard shortcut cheat sheet dialog."
                in modal.get_last_announcement()
            )

            # Dismiss via Escape key
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert not isinstance(app.screen, ShortcutCheatSheetModal)

            # Trigger via ? key
            await pilot.press("question_mark")
            await pilot.pause(0.05)
            modal = app.screen
            assert isinstance(modal, ShortcutCheatSheetModal)

            # Dismiss via ? key
            await pilot.press("question_mark")
            await pilot.pause(0.05)
            assert not isinstance(app.screen, ShortcutCheatSheetModal)

            # Trigger via F1 key
            await pilot.press("f1")
            await pilot.pause(0.05)
            modal = app.screen
            assert isinstance(modal, ShortcutCheatSheetModal)

            # Dismiss via F1 key
            await pilot.press("f1")
            await pilot.pause(0.05)
            assert not isinstance(app.screen, ShortcutCheatSheetModal)

            # Trigger via action and dismiss via Close button action / Enter key
            app.action_open_cheat_sheet()
            await pilot.pause(0.05)
            modal = app.screen
            assert isinstance(modal, ShortcutCheatSheetModal)

            await pilot.press("enter")
            await pilot.pause(0.05)
            assert not isinstance(app.screen, ShortcutCheatSheetModal)

    asyncio.run(_test())


def test_tui_shortcut_cheat_sheet_suppressed_on_text_input_focus(temp_workspace):
    """Verify shortcut cheat sheet modal is suppressed when a text input control holds focus."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)

            modal = NewFolderModal()
            app.push_screen(modal)
            await pilot.pause(0.05)

            inp = modal.query_one("#input-folder-name", Input)
            inp.focus()
            await pilot.pause(0.05)

            assert app._is_text_control_focused() is True

            # Invoking action_open_cheat_sheet when text input is focused must do nothing
            app.action_open_cheat_sheet()
            await pilot.pause(0.05)

            assert (
                app.screen is modal
            )  # Screen remains NewFolderModal, cheat sheet not opened

            # Typing ? into input field puts '?' in the input field without opening cheat sheet
            await pilot.press("question_mark")
            await pilot.pause(0.05)

            assert inp.value == "?"
            assert app.screen is modal

    asyncio.run(_test())


def test_tui_cro_forensic_modal_help_button_and_error(temp_workspace):
    """Verify CROForensicModal shows Help button and includes help_url on error."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test(size=(100, 50)) as pilot:
            modal = CROForensicModal(settings, temp_workspace)
            app.push_screen(modal)
            await pilot.pause(0.05)

            btn_help = modal.query_one("#btn-help", Button)
            assert btn_help.has_class("hidden")

            # Set invalid source path to trigger worker error
            modal.query_one("#input-source", Input).value = "/nonexistent/invalid/path"
            modal.action_run()
            await pilot.pause(0.1)

            assert not btn_help.has_class("hidden")
            assert modal.help_url == "https://docs.smartautosorter.com/troubleshooting/#cro-forensic-ingestion"

            # Trigger help action
            modal.action_help()
            await pilot.pause(0.05)
            assert "Opened documentation link:" in modal.get_last_announcement()

    asyncio.run(_test())


def test_tui_dropzone_modal_help_button_and_error(temp_workspace):
    """Verify DropZoneModal shows Help button and includes help_url on invalid path or error."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test(size=(100, 50)) as pilot:
            modal = DropZoneModal(settings, temp_workspace)
            app.push_screen(modal)
            await pilot.pause(0.05)

            btn_help = modal.query_one("#btn-help", Button)
            assert btn_help.has_class("hidden")

            # Submit invalid path
            modal.query_one("#input-drop-paths", Input).value = "/nonexistent/path/dropzone"
            modal.trigger_drop_triage()
            await pilot.pause(0.05)

            assert not btn_help.has_class("hidden")
            assert modal.help_url == "https://docs.smartautosorter.com/troubleshooting/#dropzone-errors"

            # Trigger Help action
            modal.action_help()
            await pilot.pause(0.05)
            assert "Opened documentation link:" in modal.get_last_announcement()

    asyncio.run(_test())


def test_tui_session_recovery_modal_help_action(temp_workspace):
    """Verify SessionRecoveryModal Help button triggers help link action."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    session_info = {
        "session_id": "test_sess_help",
        "base_dir": temp_workspace,
        "status": "failed",
    }

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test(size=(100, 50)) as pilot:
            modal = SessionRecoveryModal(session_info)
            app.push_screen(modal)
            await pilot.pause(0.05)

            btn_help = modal.query_one("#btn-help", Button)
            assert btn_help is not None

            modal.action_help()
            await pilot.pause(0.05)
            assert "Opened documentation link:" in modal.get_last_announcement()

    asyncio.run(_test())


def test_tui_settings_modal_help_button_on_validation_error(temp_workspace):
    """Verify SettingsModal shows Help button on concurrency validation error."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test(size=(100, 50)) as pilot:
            modal = SettingsModal(settings)
            app.push_screen(modal)
            await pilot.pause(0.05)

            btn_help = modal.query_one("#btn-help", Button)
            assert btn_help.has_class("hidden")

            # Set invalid concurrency
            modal.query_one("#input-concurrency", Input).value = "invalid_int"
            modal.action_save()
            await pilot.pause(0.05)

            assert not btn_help.has_class("hidden")
            assert modal.help_url == "https://docs.smartautosorter.com/troubleshooting/#settings-configuration"

            modal.action_help()
            await pilot.pause(0.05)
            assert "Opened documentation link:" in modal.get_last_announcement()

    asyncio.run(_test())


def test_tui_bindings_no_global_enter_and_ctrl_e_executes():
    """Verify global BINDINGS does not bind enter to execute_sort and ctrl+e binds to execute_sort."""
    bindings_by_key = {b.key: b.action for b in AutoSorterTUI.BINDINGS if hasattr(b, "key")}
    assert "enter" not in bindings_by_key or bindings_by_key["enter"] != "execute_sort"
    assert bindings_by_key.get("ctrl+e") == "execute_sort"


def test_tui_enter_key_navigates_tree_without_executing_plan(temp_workspace):
    """Verify pressing Enter on VimTree node toggles tree node without executing plan or moving files."""
    from pathlib import Path
    from unittest.mock import MagicMock

    from textual.widgets import Tree

    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    # Create dummy files
    (Path(temp_workspace) / "folder1").mkdir(exist_ok=True)
    (Path(temp_workspace) / "folder1" / "file1.txt").write_text("hello world")

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        mock_execute = MagicMock()
        app.run_execute_worker = mock_execute

        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause(0.05)
            app.plan = {"folder1": {"files": ["file1.txt"]}}
            app.app_session = MagicMock()
            app.rebuild_tree()
            await pilot.pause(0.05)

            tree = app.query_one("#plan-tree", Tree)
            tree.focus()
            await pilot.pause(0.05)

            # Get root node
            root = tree.root
            assert root is not None
            initial_expanded = root.is_expanded

            # Press Enter while tree is focused
            await pilot.press("enter")
            await pilot.pause(0.05)

            # Check that run_execute_worker was NOT called
            mock_execute.assert_not_called()

            # Check that root node toggled its expanded state
            assert root.is_expanded != initial_expanded

            # Press Ctrl+E while tree is focused to verify execution trigger
            await pilot.press("ctrl+e")
            await pilot.pause(0.05)

            mock_execute.assert_called_once()

    asyncio.run(_test())

