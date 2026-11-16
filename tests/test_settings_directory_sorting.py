"""Tests for directory sorting switches in General Settings tab."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from textual.widgets import Switch

from app.config import AppSettings
from app.core.analyzer import IncrementalAnalyzer
from app.core.mover import execute_moves
from app.ui.modals.settings import SettingsModal
from app.ui.tui import AutoSorterTUI
from app.ui.widgets.labeled_switch import LabeledSwitch

pytestmark = pytest.mark.xdist_group(name="tui")


def test_settings_modal_directory_switches_rendered_with_defaults(tmp_path):
    """Test that empty directory cleanup and hierarchy switches render with default values."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    assert settings.CLEANUP_EMPTY_FOLDERS is True
    assert settings.PRESERVE_HIERARCHY is False

    modal = SettingsModal(settings)

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=tmp_path, skip_wizard=True)
        async with app.run_test(size=(100, 50)) as pilot:
            app.push_screen(modal)
            await pilot.pause(0.1)

            sw_cleanup = modal.query_one("#switch-cleanup-empty-folders", Switch)
            sw_preserve = modal.query_one("#switch-preserve-hierarchy", Switch)

            assert sw_cleanup is not None
            assert sw_cleanup.value is True

            assert sw_preserve is not None
            assert sw_preserve.value is False

            ls_cleanup = sw_cleanup.parent
            ls_preserve = sw_preserve.parent
            assert isinstance(ls_cleanup, LabeledSwitch)
            assert isinstance(ls_preserve, LabeledSwitch)
            assert "Cleanup Empty Folders" in str(ls_cleanup.label_widget.render())
            assert "Preserve Folder Hierarchy" in str(ls_preserve.label_widget.render())

            # Test selector aliases
            assert modal.query_one("#switch-cleanup", Switch) is sw_cleanup
            assert (
                modal.query_one("#switch-cleanup-empty-directories", Switch)
                is sw_cleanup
            )
            assert modal.query_one("#switch-preserve", Switch) is sw_preserve
            assert (
                modal.query_one("#switch-preserve-folder-hierarchy", Switch)
                is sw_preserve
            )

    asyncio.run(_test())


def test_settings_modal_directory_switches_custom_initial_values(tmp_path):
    """Test that switches reflect non-default values configured in AppSettings."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.CLEANUP_EMPTY_FOLDERS = False
    settings.PRESERVE_HIERARCHY = True

    modal = SettingsModal(settings)

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=tmp_path, skip_wizard=True)
        async with app.run_test(size=(100, 50)) as pilot:
            app.push_screen(modal)
            await pilot.pause(0.1)

            sw_cleanup = modal.query_one("#switch-cleanup-empty-folders", Switch)
            sw_preserve = modal.query_one("#switch-preserve-hierarchy", Switch)

            assert sw_cleanup.value is False
            assert sw_preserve.value is True

    asyncio.run(_test())


def test_settings_modal_directory_switches_toggle_and_save(tmp_path):
    """Test toggling directory switches and confirming action_save returns updated values."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    results = []
    modal = SettingsModal(settings)

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=tmp_path, skip_wizard=True)
        async with app.run_test(size=(100, 50)) as pilot:
            app.push_screen(modal, results.append)
            await pilot.pause(0.1)

            sw_cleanup = modal.query_one("#switch-cleanup-empty-folders", Switch)
            sw_preserve = modal.query_one("#switch-preserve-hierarchy", Switch)

            sw_cleanup.value = False
            sw_preserve.value = True
            await pilot.pause(0.05)

            modal.action_save()
            await pilot.pause(0.1)

            assert len(results) == 1
            saved = results[0]
            assert saved["CLEANUP_EMPTY_FOLDERS"] is False
            assert saved["PRESERVE_HIERARCHY"] is True

    asyncio.run(_test())


def test_tui_persists_directory_sorting_switches(tmp_path):
    """Test that AutoSorterTUI on_saved persists both switch values to AppSettings."""
    settings_file = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(settings_file))
    app = AutoSorterTUI(settings=settings, base_dir=tmp_path, skip_wizard=True)

    async def _test():
        async with app.run_test(size=(100, 50)) as pilot:
            app.action_open_settings()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, SettingsModal)

            sw_cleanup = modal.query_one("#switch-cleanup-empty-folders", Switch)
            sw_preserve = modal.query_one("#switch-preserve-hierarchy", Switch)

            sw_cleanup.value = False
            sw_preserve.value = True
            await pilot.pause(0.05)

            modal.action_save()
            await pilot.pause(0.1)

            assert app.settings.CLEANUP_EMPTY_FOLDERS is False
            assert app.settings.PRESERVE_HIERARCHY is True

            # Flush debounce timer and verify persistence across reload
            app.settings.flush()
            reloaded = AppSettings(filepath=str(settings_file))
            assert reloaded.CLEANUP_EMPTY_FOLDERS is False
            assert reloaded.PRESERVE_HIERARCHY is True

    asyncio.run(_test())


def test_mover_respects_cleanup_switch_setting(tmp_path):
    """Verify execute_moves respects CLEANUP_EMPTY_FOLDERS setting."""
    d_clean = tmp_path / "clean_empty"
    d_clean.mkdir()
    d_no_clean = tmp_path / "keep_empty"
    d_no_clean.mkdir()

    node_clean = {
        "__type__": "directory",
        "source_path": str(d_clean),
        "status": "To Be Deleted",
        "protected": False,
    }
    node_no_clean = {
        "__type__": "directory",
        "source_path": str(d_no_clean),
        "status": "To Be Deleted",
        "protected": False,
    }

    mock_db = MagicMock()
    mock_history = MagicMock()

    class SettingsNoCleanup:
        CLEANUP_EMPTY_FOLDERS = False
        PROTECTED_PATHS: list[str] = []

    class SettingsWithCleanup:
        CLEANUP_EMPTY_FOLDERS = True
        PROTECTED_PATHS: list[str] = []

    # When CLEANUP_EMPTY_FOLDERS is False, folder should remain
    execute_moves(
        str(tmp_path),
        {"keep_empty": node_no_clean},
        mock_db,
        mock_history,
        SettingsNoCleanup(),
    )
    assert d_no_clean.exists()

    # When CLEANUP_EMPTY_FOLDERS is True, empty folder should be removed
    summary = execute_moves(
        str(tmp_path),
        {"clean_empty": node_clean},
        mock_db,
        mock_history,
        SettingsWithCleanup(),
    )
    assert not d_clean.exists()
    assert summary["deleted_folders"] == 1


def test_analyzer_respects_preserve_hierarchy_setting():
    """Verify IncrementalAnalyzer respects PRESERVE_HIERARCHY setting."""
    from app.core.analyzer import clustering_registry

    mock_db = MagicMock()
    mock_db.get_all_documents.return_value = [
        ("nested/sub/doc1.txt", "Financial statement for Q1.", "hash1", None),
    ]

    with patch("app.core.semantic_embeddings.SemanticEmbeddingManager"):
        analyzer = IncrementalAnalyzer(
            max_folders=3,
            stop_words={"the", "and"},
            db=mock_db,
            model_path=None,
            strategy_name="default",
        )

    class SettingsNoHierarchy:
        PRESERVE_HIERARCHY = False
        MAX_DEPTH = 5
        MAX_FEATURES = 3
        CONTEXTUAL_RENAMING = False
        KEYWORD_RULES = {}
        LEARNED_RULES = {}
        EXTENSION_CATEGORIES = {}
        POLICIES = []

    class SettingsWithHierarchy:
        PRESERVE_HIERARCHY = True
        MAX_DEPTH = 5
        MAX_FEATURES = 3
        CONTEXTUAL_RENAMING = False
        KEYWORD_RULES = {}
        LEARNED_RULES = {}
        EXTENSION_CATEGORIES = {}
        POLICIES = []

    strat = clustering_registry.get_strategy("default")
    with patch.object(strat, "generate_plan") as mock_gen:
        mock_gen.return_value = (
            {"Finance": {"nested/sub/doc1.txt": {"__type__": "file"}}},
            0.0,
        )

        with patch.object(
            analyzer, "_inject_hierarchy", wraps=analyzer._inject_hierarchy
        ) as spy_no_hier:
            plan_no_hier = analyzer.generate_sorting_plan(
                "dummy_dir", runtime_settings=SettingsNoHierarchy()
            )
            calls_no_hier = spy_no_hier.call_count

        with patch.object(
            analyzer, "_inject_hierarchy", wraps=analyzer._inject_hierarchy
        ) as spy_with_hier:
            plan_with_hier = analyzer.generate_sorting_plan(
                "dummy_dir", runtime_settings=SettingsWithHierarchy()
            )
            calls_with_hier = spy_with_hier.call_count

        assert calls_with_hier > calls_no_hier
        assert "Finance" in plan_with_hier
        assert "Finance" in plan_no_hier
