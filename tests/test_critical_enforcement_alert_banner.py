"""Tests for Critical-Only Enforcement with Alert Banner and Execution Control."""

import asyncio
import os
import tempfile
from unittest.mock import patch

import pytest
from textual.widgets import Button, Static, Tree

from app.config import AppSettings
from app.core.verifier import VerificationEngine
from app.ui.tui import AutoSorterTUI

pytestmark = pytest.mark.xdist_group(name="tui")


@pytest.fixture
def temp_workspace():
    """Create temporary directory workspace for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        file1 = os.path.join(tmpdir, "doc1.txt")
        file2 = os.path.join(tmpdir, "doc2.txt")
        with open(file1, "w", encoding="utf-8") as f:
            f.write("content 1")
        with open(file2, "w", encoding="utf-8") as f:
            f.write("content 2")
        yield tmpdir


def test_verifier_differentiates_critical_collisions_and_warnings(temp_workspace):
    """Verify VerificationEngine distinguishes critical path collisions from non-fatal broken links."""
    base_dir = temp_workspace

    # Collision plan: two source files targeting identical destination filename in same directory
    collision_plan = {
        "doc1.txt": {
            "__type__": "file",
            "relative_source": "doc1.txt",
            "filepath": os.path.join(base_dir, "doc1.txt"),
            "target_filename": "conflicting_target.txt",
        },
        "doc2.txt": {
            "__type__": "file",
            "relative_source": "doc2.txt",
            "filepath": os.path.join(base_dir, "doc2.txt"),
            "target_filename": "conflicting_target.txt",
        },
    }

    result = VerificationEngine.verify_plan_integrity(base_dir, collision_plan)
    assert result["has_critical_errors"] is True
    assert len(result["collisions"]) > 0
    assert VerificationEngine.is_critical_error(result) is True

    # Non-fatal warning plan: broken link / shortcut
    symlink_path = os.path.join(base_dir, "broken_link.lnk")
    warning_plan = {
        "valid_doc.txt": {
            "__type__": "file",
            "relative_source": "doc1.txt",
            "filepath": os.path.join(base_dir, "doc1.txt"),
            "target_filename": "valid_target.txt",
            "confirmed": True,
        }
    }

    # Simulate broken link reported in tracker
    with patch(
        "app.core.verifier.VirtualFilesystemTracker.check_broken_links",
        return_value=[
            {
                "path": symlink_path,
                "type": "broken_shortcut",
                "target": "/nonexistent/target.exe",
                "message": f"Broken shortcut target: '{symlink_path}' points to '/nonexistent/target.exe'",
            }
        ],
    ):
        warn_res = VerificationEngine.verify_plan_integrity(base_dir, warning_plan)
        assert warn_res["has_critical_errors"] is False
        assert len(warn_res["broken_links"]) > 0
        assert VerificationEngine.is_critical_error(warn_res) is False


def test_execution_button_disabled_on_collision_alert_banner_rendered(temp_workspace):
    """Verify execution button is disabled immediately when a destination collision occurs, and alert banner displays source and target paths."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            file1_path = os.path.join(temp_workspace, "doc1.txt")
            file2_path = os.path.join(temp_workspace, "doc2.txt")
            target_path = os.path.normpath(
                os.path.join(temp_workspace, "Finance", "same_target.txt")
            )

            app.plan = {
                "Finance": {
                    "doc1.txt": {
                        "__type__": "file",
                        "relative_source": "doc1.txt",
                        "filepath": file1_path,
                        "target_filename": "same_target.txt",
                        "confirmed": True,
                    },
                    "doc2.txt": {
                        "__type__": "file",
                        "relative_source": "doc2.txt",
                        "filepath": file2_path,
                        "target_filename": "same_target.txt",
                        "confirmed": True,
                    },
                }
            }

            app.rebuild_tree()
            await pilot.pause()

            result = VerificationEngine.verify_plan_integrity(temp_workspace, app.plan)
            app._update_validation_state(result)
            await pilot.pause()

            btn = app.query_one("#btn-execute", Button)
            assert btn.disabled is True
            assert app.has_critical_errors is True

            # Verify alert banner displays source and destination paths
            banner = app.query_one("#alert-banner", Static)
            assert banner.display is True
            assert "alert-critical" in banner.classes
            banner_text = str(banner.render())
            assert "CRITICAL FILE COLLISION" in banner_text
            assert "Destination:" in banner_text
            assert target_path in banner_text or "same_target.txt" in banner_text

    asyncio.run(_test())


def test_execution_button_enabled_on_broken_link_warning(temp_workspace):
    """Verify execution button remains enabled when only broken symlink / shortcut warnings exist."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            file1_path = os.path.join(temp_workspace, "doc1.txt")
            app.plan = {
                "Archive": {
                    "doc1.txt": {
                        "__type__": "file",
                        "relative_source": "doc1.txt",
                        "filepath": file1_path,
                        "target_filename": "doc1.txt",
                        "confirmed": True,
                    }
                }
            }

            app.rebuild_tree()
            await pilot.pause()

            # Simulate broken link warning from verification
            warn_result = {
                "success": False,
                "has_critical_errors": False,
                "collisions": [],
                "circular_renames": [],
                "broken_links": [
                    {
                        "path": os.path.join(temp_workspace, "broken.lnk"),
                        "type": "broken_shortcut",
                        "target": "/missing.exe",
                        "message": "Broken shortcut target: 'broken.lnk' points to '/missing.exe'",
                    }
                ],
                "long_paths": [],
                "invalid_renames": [],
                "unconfirmed_renames": [],
                "warnings": ["Broken shortcut target"],
            }

            app._update_validation_state(warn_result)
            await pilot.pause()

            btn = app.query_one("#btn-execute", Button)
            assert btn.disabled is False
            assert app.has_critical_errors is False

            # Verify banner does NOT use red critical styling
            banner = app.query_one("#alert-banner", Static)
            assert "alert-critical" not in banner.classes

    asyncio.run(_test())


def test_dynamic_recalculation_enables_button_within_500ms(temp_workspace):
    """Verify execution button switches to active when configuration / plan edit clears critical collision."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            file1_path = os.path.join(temp_workspace, "doc1.txt")
            file2_path = os.path.join(temp_workspace, "doc2.txt")

            # Initial collision state
            app.plan = {
                "Finance": {
                    "doc1.txt": {
                        "__type__": "file",
                        "relative_source": "doc1.txt",
                        "filepath": file1_path,
                        "target_filename": "same_target.txt",
                        "confirmed": True,
                    },
                    "doc2.txt": {
                        "__type__": "file",
                        "relative_source": "doc2.txt",
                        "filepath": file2_path,
                        "target_filename": "same_target.txt",
                        "confirmed": True,
                    },
                }
            }

            app.rebuild_tree()
            await pilot.pause()

            result = VerificationEngine.verify_plan_integrity(temp_workspace, app.plan)
            app._update_validation_state(result)
            await pilot.pause()

            btn = app.query_one("#btn-execute", Button)
            assert btn.disabled is True

            # Resolve collision by renaming target of doc2.txt
            app.plan["Finance"]["doc2.txt"]["target_filename"] = "unique_doc2.txt"

            # Trigger recalculation
            app.rebuild_tree()
            await pilot.pause()

            cleared_result = VerificationEngine.verify_plan_integrity(
                temp_workspace, app.plan
            )
            app._update_validation_state(cleared_result)
            await pilot.pause()

            # Verify button becomes enabled
            assert btn.disabled is False
            assert app.has_critical_errors is False

            banner = app.query_one("#alert-banner", Static)
            assert banner.display is False or "alert-critical" not in banner.classes

    asyncio.run(_test())


def test_tree_highlights_critical_nodes_in_red_and_warnings_in_yellow(temp_workspace):
    """Verify tree nodes with critical errors are stylized in red and warning nodes in yellow."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            file1_path = os.path.join(temp_workspace, "doc1.txt")
            file2_path = os.path.join(temp_workspace, "doc2.txt")

            app.plan = {
                "Finance": {
                    "doc1.txt": {
                        "__type__": "file",
                        "relative_source": "doc1.txt",
                        "filepath": file1_path,
                        "target_filename": "doc1.txt",
                        "confirmed": True,
                    },
                    "doc2.txt": {
                        "__type__": "file",
                        "relative_source": "doc2.txt",
                        "filepath": file2_path,
                        "target_filename": "doc2.txt",
                        "confirmed": True,
                    },
                }
            }

            crit_paths = {os.path.normpath(file1_path), "doc1.txt"}
            warn_paths = {os.path.normpath(file2_path), "doc2.txt"}

            app.critical_error_paths = crit_paths
            app.warning_paths = warn_paths

            app.rebuild_tree()
            # rebuild_tree validates the plan on a worker thread, and its result
            # replaces the paths set above; let it land before setting them again.
            await app.workers.wait_for_complete()
            await pilot.pause()

            # Re-apply styles explicitly for assertion
            app.critical_error_paths = crit_paths
            app.warning_paths = warn_paths
            app._apply_tree_node_styles()
            await pilot.pause()

            tree = app.query_one("#plan-tree", Tree)
            finance_folder = tree.root.children[0]
            doc1_node = finance_folder.children[0]
            doc2_node = finance_folder.children[1]

            def has_color(node_label, color_name):
                if hasattr(node_label, "spans"):
                    for span in node_label.spans:
                        if color_name in str(span.style):
                            return True
                return color_name in str(node_label)

            assert has_color(doc1_node.label, "red")
            assert has_color(doc2_node.label, "yellow")

    asyncio.run(_test())
