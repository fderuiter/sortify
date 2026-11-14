"""Automated test suite for integrated destination override and inline collision detection."""

import asyncio
import os
import time

from textual.widgets import Input, Label, Select

from app.config import AppSettings
from app.core.path_utils import _is_absolute_path, sanitize_folder_key, sanitize_plan
from app.core.verifier import VerificationEngine
from app.ui.modals.edit import ReassignModal, RenameModal, RenameResult
from app.ui.tui import AutoSorterTUI


def test_rename_result_properties():
    """Verify RenameResult preserves string semantics and stores destination override."""
    res = RenameResult("new_file.txt", destination_override="/Volumes/Backup")
    assert isinstance(res, str)
    assert res == "new_file.txt"
    assert res.name == "new_file.txt"
    assert res.destination_override == "/Volumes/Backup"
    assert res.to_tuple() == ("new_file.txt", "/Volumes/Backup")


def test_is_absolute_path_detection():
    """Verify _is_absolute_path recognizes POSIX roots, Windows drives, and UNC shares."""
    assert _is_absolute_path("/var/log") is True
    assert _is_absolute_path("/Volumes/External/Backup") is True
    assert _is_absolute_path("C:\\Users\\Sortify") is True
    assert _is_absolute_path("D:/Drive/Archive") is True
    assert _is_absolute_path(r"\\server\share\folder") is True

    assert _is_absolute_path("relative/folder") is False
    assert _is_absolute_path("Subfolder") is False
    assert _is_absolute_path("") is False
    assert _is_absolute_path(None) is False


def test_sanitize_folder_key_preserves_absolute_paths():
    """Verify sanitize_folder_key accepts and preserves validated absolute paths."""
    posix_path = "/Volumes/External/Backup_2026"
    safe_posix, transformed = sanitize_folder_key(posix_path)
    assert safe_posix == posix_path
    assert transformed is False

    win_path = r"D:\External\Backup_2026"
    safe_win, transformed_win = sanitize_folder_key(win_path)
    assert safe_win == win_path
    assert transformed_win is False

    # Traversal segments in absolute path must be stripped
    traversal_path = "/Volumes/Backup/../etc/passwd"
    safe_traversal, trans_trav = sanitize_folder_key(traversal_path)
    assert trans_trav is True
    assert ".." not in safe_traversal
    assert safe_traversal == "/Volumes/Backup/etc/passwd"


def test_sanitize_plan_preserves_absolute_destination_keys():
    """Verify sanitize_plan preserves absolute destination directories without flattening."""
    raw_plan = {
        "/Volumes/Backup/Finance": {
            "invoice.pdf": {
                "__type__": "file",
                "target_filename": "invoice.pdf",
                "relative_source": "invoice.pdf",
            }
        },
        r"D:\Archive\Reports": {
            "report.docx": {
                "__type__": "file",
                "target_filename": "report.docx",
                "relative_source": "report.docx",
            }
        },
    }
    sanitized, warnings = sanitize_plan(raw_plan)
    assert "/Volumes/Backup/Finance" in sanitized
    assert r"D:\Archive\Reports" in sanitized
    assert "invoice.pdf" in sanitized["/Volumes/Backup/Finance"]
    assert "report.docx" in sanitized[r"D:\Archive\Reports"]


def test_verifier_get_moves_handles_absolute_destination_paths(tmp_path):
    """Verify VerificationEngine.get_moves handles absolute destination directories correctly."""
    base_dir = str(tmp_path)
    plan = {
        "/Volumes/ExternalDrive/Backup": {
            "data.csv": {
                "__type__": "file",
                "target_filename": "data.csv",
                "relative_source": "data.csv",
            }
        },
        "LocalCategory": {
            "remote.pdf": {
                "__type__": "file",
                "target_filename": "remote.pdf",
                "relative_source": "remote.pdf",
                "destination_override": "/mnt/external/remote.pdf",
            }
        },
    }
    moves = VerificationEngine.get_moves(base_dir, plan)
    moves_map = {key: dst for key, src, dst in moves}

    assert moves_map["data.csv"] == os.path.normpath(
        "/Volumes/ExternalDrive/Backup/data.csv"
    )
    assert moves_map["remote.pdf"] == os.path.normpath(
        "/mnt/external/remote.pdf/remote.pdf"
    )


def test_rename_modal_collision_detection_and_latency():
    """Verify RenameModal flags live collisions across staging plan within 50ms."""
    plan = {
        "Documents": {
            "existing.txt": {
                "__type__": "file",
                "filepath": "/workspace/existing.txt",
                "target_filename": "existing.txt",
            }
        }
    }
    modal = RenameModal(
        title="Rename File: doc.txt",
        current_name="doc",
        extension=".txt",
        category="Documents",
        plan=plan,
        base_dir="/workspace",
        source_path="/workspace/doc.txt",
    )

    # Initial state: no collision
    t0 = time.perf_counter()
    blocked, msg = modal._check_collision_and_update_ui()
    dt_ms = (time.perf_counter() - t0) * 1000
    assert dt_ms < 50, f"Collision check took {dt_ms:.2f}ms (>50ms requirement)"
    assert blocked is False
    assert msg is None

    # Collision when changing name to existing.txt
    modal.query_one = lambda selector, *args: (
        type("Inp", (), {"value": "existing"})()
        if selector == "#input-name"
        else type("Inp", (), {"value": ""})()
    )
    t0 = time.perf_counter()
    blocked, msg = modal._check_collision_and_update_ui()
    dt_ms = (time.perf_counter() - t0) * 1000
    assert dt_ms < 50
    assert blocked is False
    assert msg is not None
    assert "Collision detected" in msg
    assert "existing.txt" in msg

    # Security error on directory traversal
    modal.query_one = lambda selector, *args: (
        type("Inp", (), {"value": "../malicious"})()
        if selector == "#input-name"
        else type("Inp", (), {"value": ""})()
    )
    blocked, msg = modal._check_collision_and_update_ui()
    assert blocked is True
    assert "Directory traversal" in msg


def test_reassign_modal_collision_detection_and_latency():
    """Verify ReassignModal flags duplicate filenames in candidate target folder within 50ms."""
    plan = {
        "Archive": {
            "file1.pdf": {
                "__type__": "file",
                "filepath": "/workspace/Archive/file1.pdf",
                "target_filename": "file1.pdf",
            }
        }
    }
    target_files = [
        {
            "key": "file1.pdf",
            "target_filename": "file1.pdf",
            "filepath": "/workspace/Inbox/file1.pdf",
        }
    ]
    modal = ReassignModal(
        existing_folders=["Archive", "Drafts"],
        count=1,
        plan=plan,
        base_dir="/workspace",
        target_files=target_files,
    )

    modal.query_one = lambda selector, *args: (
        type("Inp", (), {"value": "Archive"})()
        if selector == "#input-target-folder"
        else type("Sel", (), {"value": Select.BLANK})()
    )

    t0 = time.perf_counter()
    blocked, msg = modal._check_collision_and_update_ui()
    dt_ms = (time.perf_counter() - t0) * 1000
    assert dt_ms < 50, f"Collision check took {dt_ms:.2f}ms (>50ms requirement)"
    assert blocked is False
    assert msg is not None
    assert "Collision detected" in msg
    assert "file1.pdf" in msg


def test_reassign_files_disambiguates_duplicate_names(tmp_path):
    """Verify reassigning files to a folder with identical filenames creates disambiguated keys."""
    temp_workspace = str(tmp_path)

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            file_a_path = os.path.join(temp_workspace, "Inbox", "data.csv")
            file_b_path = os.path.join(temp_workspace, "Archive", "data.csv")
            app.plan = {
                "Inbox": {
                    "data.csv": {
                        "__type__": "file",
                        "filepath": file_a_path,
                        "target_filename": "data.csv",
                        "folder": "Inbox",
                        "is_checked": True,
                    }
                },
                "Archive": {
                    "data.csv": {
                        "__type__": "file",
                        "filepath": file_b_path,
                        "target_filename": "data.csv",
                        "folder": "Archive",
                        "is_checked": False,
                    }
                },
            }
            app.rebuild_tree()

            # Select file in Inbox
            tree = app.query_one("#plan-tree")
            # Navigate to child node of Inbox
            inbox_node = [n for n in tree.root.children if "Inbox" in str(n.label)][0]
            inbox_file_node = inbox_node.children[0]
            app.active_tree_node = inbox_file_node

            app.action_reassign_selected()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, ReassignModal)

            # Reassign to Archive
            inp = modal.query_one("#input-target-folder", Input)
            inp.value = "Archive"
            await pilot.pause(0.1)

            # Verify collision warning was displayed inline
            warn_lbl = modal.query_one("#warning-collision", Label)
            assert "Collision detected" in str(warn_lbl.render())

            await pilot.press("enter")
            await pilot.pause(0.1)

            # Both files must exist in Archive without clobbering!
            archive_files = app.plan["Archive"]
            assert len(archive_files) == 2
            assert "data.csv" in archive_files
            assert "data_1.csv" in archive_files
            assert archive_files["data_1.csv"]["target_filename"] == "data_1.csv"
            assert "data.csv" not in app.plan.get("Inbox", {})

    asyncio.run(_test())


def test_rename_folder_merges_recursively_without_clobbering(tmp_path):
    """Verify renaming a folder to an existing folder merges dictionary entries in self.plan."""
    temp_workspace = str(tmp_path)

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            file1_path = os.path.join(temp_workspace, "FolderA", "doc.txt")
            file2_path = os.path.join(temp_workspace, "FolderB", "doc.txt")
            file3_path = os.path.join(temp_workspace, "FolderB", "notes.pdf")

            app.plan = {
                "FolderA": {
                    "doc.txt": {
                        "__type__": "file",
                        "filepath": file1_path,
                        "target_filename": "doc.txt",
                        "folder": "FolderA",
                    }
                },
                "FolderB": {
                    "doc.txt": {
                        "__type__": "file",
                        "filepath": file2_path,
                        "target_filename": "doc.txt",
                        "folder": "FolderB",
                    },
                    "notes.pdf": {
                        "__type__": "file",
                        "filepath": file3_path,
                        "target_filename": "notes.pdf",
                        "folder": "FolderB",
                    },
                },
            }
            app.rebuild_tree()

            tree = app.query_one("#plan-tree")
            folder_a_node = [
                n for n in tree.root.children if "FolderA" in str(n.label)
            ][0]
            app.active_tree_node = folder_a_node

            app.action_rename_node()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, RenameModal)

            # Rename FolderA -> FolderB
            inp = modal.query_one("#input-name", Input)
            inp.value = "FolderB"
            await pilot.pause(0.1)

            # Check collision warning
            warn_lbl = modal.query_one("#warning-collision", Label)
            assert "Folder 'FolderB' already exists" in str(warn_lbl.render())

            await pilot.press("enter")
            await pilot.pause(0.1)

            # FolderA is merged into FolderB
            assert "FolderA" not in app.plan
            folder_b_contents = app.plan["FolderB"]
            # Total 3 files: original doc.txt, disambiguated doc_1.txt, and notes.pdf
            assert len(folder_b_contents) == 3
            assert "doc.txt" in folder_b_contents
            assert "doc_1.txt" in folder_b_contents
            assert "notes.pdf" in folder_b_contents

    asyncio.run(_test())


def test_rename_file_with_destination_override_external_path(tmp_path):
    """Verify renaming a file with absolute external destination path override."""
    temp_workspace = str(tmp_path)

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            file_path = os.path.join(temp_workspace, "Inbox", "scan.pdf")
            app.plan = {
                "Inbox": {
                    "scan.pdf": {
                        "__type__": "file",
                        "filepath": file_path,
                        "target_filename": "scan.pdf",
                        "folder": "Inbox",
                    }
                }
            }
            app.rebuild_tree()

            tree = app.query_one("#plan-tree")
            inbox_node = [n for n in tree.root.children if "Inbox" in str(n.label)][0]
            scan_file_node = inbox_node.children[0]
            app.active_tree_node = scan_file_node

            app.action_rename_node()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, RenameModal)

            # Rename file to document_final and override destination to /Volumes/Backup/External
            name_inp = modal.query_one("#input-name", Input)
            name_inp.value = "document_final"
            dest_inp = modal.query_one("#input-destination", Input)
            dest_inp.value = "/Volumes/Backup/External"
            await pilot.pause(0.1)

            await pilot.press("enter")
            await pilot.pause(0.1)

            # File should be removed from Inbox and staged in /Volumes/Backup/External
            assert "scan.pdf" not in app.plan.get("Inbox", {})
            assert "/Volumes/Backup/External" in app.plan
            staged = app.plan["/Volumes/Backup/External"]["document_final.pdf"]
            assert staged["target_filename"] == "document_final.pdf"
            assert staged["folder"] == "/Volumes/Backup/External"

    asyncio.run(_test())
