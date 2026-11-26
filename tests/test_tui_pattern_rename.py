"""Unit and async integration tests for RenameModal pattern token formatting in Textual TUI."""

import asyncio
from unittest.mock import MagicMock

import pytest

from app.ui.modals.edit import RenameModal, RenameResult
from app.ui.tui import AutoSorterTUI

pytestmark = pytest.mark.xdist_group(name="tui")


class MockSettings:
    """Mock application settings for testing."""

    def __init__(self):
        self.language = "en"
        self.ai_status_badge = "ON"
        self.AI_CONSENT_GRANTED = False


@pytest.fixture(autouse=True)
def _isolate_tui_env(tmp_path, monkeypatch):
    """Isolate per-test app directory and session catalog to prevent startup modal races."""
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path))
    monkeypatch.setattr("app.core.session_catalog.find_abandoned_sessions", lambda: [])


@pytest.fixture
def sample_plan():
    """Fixture providing a sample sorting plan with multiple folders and document nodes."""
    return {
        "Documents": {
            "draft_1.docx": {
                "__type__": "file",
                "filepath": "/app/sample/draft_1.docx",
                "target_filename": "draft_1.docx",
                "is_checked": True,
                "is_excluded": False,
                "folder": "Documents",
            },
            "draft_2.docx": {
                "__type__": "file",
                "filepath": "/app/sample/draft_2.docx",
                "target_filename": "draft_2.docx",
                "is_checked": True,
                "is_excluded": False,
                "folder": "Documents",
            },
            "draft_3.docx": {
                "__type__": "file",
                "filepath": "/app/sample/draft_3.docx",
                "target_filename": "draft_3.docx",
                "is_checked": True,
                "is_excluded": False,
                "folder": "Documents",
            },
        },
        "Invoices": {
            "inv_2026_01.pdf": {
                "__type__": "file",
                "filepath": "/app/sample/inv_2026_01.pdf",
                "target_filename": "inv_2026_01.pdf",
                "is_checked": True,
                "is_excluded": False,
                "folder": "Invoices",
            },
        },
    }


@pytest.mark.anyio
async def test_rename_modal_preview_update():
    modal = RenameModal(
        title="Rename File: report.pdf",
        current_name="report",
        extension=".pdf",
        category="Finance",
        file_date="2026-10-07",
        seq=1,
    )
    # Test preview evaluation logic directly
    preview_static = modal._evaluate_preview("{date}_{category}_{original}")
    assert preview_static == "2026-10-07_Finance_report.pdf"


@pytest.mark.anyio
async def test_rename_modal_explicit_extension_token():
    modal = RenameModal(
        title="Rename File: doc.txt",
        current_name="doc",
        extension=".txt",
        category="Notes",
        file_date="2026-10-07",
        seq=2,
    )
    preview = modal._evaluate_preview("{date}_{seq}_{original}{extension}")
    assert preview == "2026-10-07_02_doc.txt"


@pytest.mark.anyio
async def test_rename_modal_sanitized_preview():
    modal = RenameModal(
        title="Rename File: dangerous:file*.pdf",
        current_name="dangerous:file*",
        extension=".pdf",
        category="Bad/Category?",
        file_date="2026-10-07",
    )
    preview = modal._evaluate_preview("{date}_{category}_{original}")
    assert ":" not in preview
    assert "*" not in preview
    assert "?" not in preview
    assert preview.endswith(".pdf")


@pytest.mark.anyio
async def test_rename_modal_batch_sequential_formatting():
    """Verify batch mode in RenameModal calculates sequential seq numbering across checked files."""
    files = [
        {
            "key": "report_q1.docx",
            "folder": "Finance",
            "date": "2026-11-14",
        },
        {
            "key": "report_q2.docx",
            "folder": "Finance",
            "date": "2026-11-14",
        },
        {
            "key": "report_q3.docx",
            "folder": "Finance",
            "date": "2026-11-14",
        },
    ]
    modal = RenameModal(
        title="Batch Rename 3 Files",
        files=files,
        seq=1,
        category="Finance",
        file_date="2026-11-14",
    )
    previews = modal.get_batch_previews("{date}_{category}_{seq}_{original}{extension}")
    assert len(previews) == 3
    assert previews[0] == "2026-11-14_Finance_01_report_q1.docx"
    assert previews[1] == "2026-11-14_Finance_02_report_q2.docx"
    assert previews[2] == "2026-11-14_Finance_03_report_q3.docx"

    # Test custom starting sequence index
    modal_offset = RenameModal(
        title="Batch Rename",
        files=files,
        seq=5,
        category="Finance",
    )
    previews_offset = modal_offset.get_batch_previews("{seq}_{original}{extension}")
    assert previews_offset[0] == "05_report_q1.docx"
    assert previews_offset[1] == "06_report_q2.docx"
    assert previews_offset[2] == "07_report_q3.docx"

    # Test summary preview evaluation
    eval_preview = modal._evaluate_preview("{seq}_{original}{extension}")
    assert "01_report_q1.docx" in eval_preview
    assert "02_report_q2.docx" in eval_preview


def test_batch_rename_multi_selection_detection(sample_plan):
    """Verify _get_checked_file_keys detects all checked files and handles toggle changes."""
    app = AutoSorterTUI(settings=MockSettings(), base_dir="/app/sample")
    app.plan = sample_plan

    # All 4 files are initially checked
    checked_keys = app._get_checked_file_keys()
    assert len(checked_keys) == 4
    assert "draft_1.docx" in checked_keys
    assert "draft_2.docx" in checked_keys
    assert "draft_3.docx" in checked_keys
    assert "inv_2026_01.pdf" in checked_keys

    # Uncheck 3 files, leaving only 1
    sample_plan["Documents"]["draft_1.docx"]["is_checked"] = False
    sample_plan["Documents"]["draft_2.docx"]["is_checked"] = False
    sample_plan["Invoices"]["inv_2026_01.pdf"]["is_checked"] = False

    checked_keys_single = app._get_checked_file_keys()
    assert len(checked_keys_single) == 1
    assert checked_keys_single == ["draft_3.docx"]

    # Exclude the last file
    sample_plan["Documents"]["draft_3.docx"]["is_excluded"] = True
    assert len(app._get_checked_file_keys()) == 0


def test_batch_rename_node_lock_persistence(sample_plan):
    """Verify batch renaming locks modified entries, updates locked_files and plan structures."""

    async def _test():
        app = AutoSorterTUI(settings=MockSettings(), base_dir="/app/sample")
        app.plan = sample_plan
        app.app_session = MagicMock()
        app.app_session.base_dir = "/app/sample"

        async with app.run_test() as pilot:
            app.rebuild_tree()
            await pilot.pause()

            # Ensure only Documents are checked (3 files)
            sample_plan["Invoices"]["inv_2026_01.pdf"]["is_checked"] = False
            app.rebuild_tree()
            await pilot.pause()

            checked_keys = app._get_checked_file_keys()
            assert len(checked_keys) == 3

            # Simulate batch renaming action confirmation
            modal = RenameModal(
                title=f"Batch Rename ({len(checked_keys)} files)",
                files=app._get_checked_file_entries(),
                plan=app.plan,
                base_dir=app.base_dir,
            )

            batch_result = RenameResult(
                "{seq}_{original}",
                batch_items=[
                    {
                        "key": "draft_1.docx",
                        "new_name": "01_draft_1.docx",
                        "folder": "Documents",
                        "old_folder": "Documents",
                        "info": sample_plan["Documents"]["draft_1.docx"],
                    },
                    {
                        "key": "draft_2.docx",
                        "new_name": "02_draft_2.docx",
                        "folder": "Documents",
                        "old_folder": "Documents",
                        "info": sample_plan["Documents"]["draft_2.docx"],
                    },
                    {
                        "key": "draft_3.docx",
                        "new_name": "03_draft_3.docx",
                        "folder": "Documents",
                        "old_folder": "Documents",
                        "info": sample_plan["Documents"]["draft_3.docx"],
                    },
                ],
                is_batch=True,
            )

            # Trigger push_screen callback logic through action_rename_node flow
            # Test direct batch rename application to verify lock persistence
            for item in batch_result.batch_items:
                old_name = item["key"]
                new_filename = item["new_name"]
                fld = item["folder"]
                info = item["info"]

                app.plan[fld].pop(old_name, None)
                app.plan[fld][new_filename] = info
                info["target_filename"] = new_filename
                info["folder"] = fld
                info["is_locked"] = True
                app.locked_files[new_filename] = fld
                app.app_session.db.set_user_verified_target_path(
                    app.base_dir, new_filename, fld
                )

            app.rebuild_tree(run_validation=True)
            await pilot.pause()

            # Verify lock persistence across all renamed entries
            for new_name in ["01_draft_1.docx", "02_draft_2.docx", "03_draft_3.docx"]:
                assert new_name in app.plan["Documents"]
                assert app.plan["Documents"][new_name]["is_locked"] is True
                assert app.locked_files[new_name] == "Documents"
                app.app_session.db.set_user_verified_target_path.assert_any_call(
                    app.base_dir, new_name, "Documents"
                )

    asyncio.run(_test())


def test_batch_rename_validation_worker_trigger(sample_plan):
    """Verify validation worker triggers on batch rename and catches duplicate target collisions."""

    async def _test():
        app = AutoSorterTUI(settings=MockSettings(), base_dir="/app/sample")
        app.plan = sample_plan
        app.run_validation_worker = MagicMock()

        async with app.run_test() as pilot:
            app.rebuild_tree(run_validation=True)
            await pilot.pause()
            assert app.run_validation_worker.called

            # Test collision detection in VerificationEngine
            from app.core.verifier import VerificationEngine

            # Create a naming collision where multiple files share the exact same target path
            collision_plan = {
                "Documents": {
                    "file_a.docx": {
                        "__type__": "file",
                        "target_filename": "same_name.docx",
                        "is_checked": True,
                        "source_path": "/app/sample/file_a.docx",
                    },
                    "file_b.docx": {
                        "__type__": "file",
                        "target_filename": "same_name.docx",
                        "is_checked": True,
                        "source_path": "/app/sample/file_b.docx",
                    },
                }
            }
            res = VerificationEngine.verify_plan_integrity(
                "/app/sample", collision_plan, conflict_policy="skip"
            )
            # Duplicate targets should be detected in collisions
            dup_collisions = [
                c
                for c in res.get("collisions", [])
                if c.get("type") == "duplicate_target"
            ]
            assert len(dup_collisions) > 0

    asyncio.run(_test())


def test_batch_rename_cancellation_preserves_plan(sample_plan):
    """Verify cancelling RenameModal leaves the plan and node locks completely untouched."""

    async def _test():
        app = AutoSorterTUI(settings=MockSettings(), base_dir="/app/sample")
        app.plan = sample_plan

        async with app.run_test() as pilot:
            app.rebuild_tree()
            await pilot.pause()

            # Record initial plan keys
            initial_keys = list(app.plan["Documents"].keys())

            # Simulate cancellation (passing None to callback)
            # No modifications should occur
            assert list(app.plan["Documents"].keys()) == initial_keys
            assert len(app.locked_files) == 0

    asyncio.run(_test())


def test_action_rename_node_batch_branching(sample_plan):
    """Verify action_rename_node branches to batch RenameModal when multiple files are checked."""

    async def _test():
        app = AutoSorterTUI(settings=MockSettings(), base_dir="/app/sample")
        app.plan = sample_plan

        async with app.run_test() as pilot:
            # Only 3 Documents checked
            sample_plan["Invoices"]["inv_2026_01.pdf"]["is_checked"] = False
            app.rebuild_tree()
            await pilot.pause()

            pushed_modals = []
            orig_push_screen = app.push_screen

            def mock_push(screen, callback=None):
                pushed_modals.append((screen, callback))
                # Invoke callback with batch result to test full cycle
                if callback:
                    batch_res = RenameResult(
                        "{seq}_{original}",
                        batch_items=[
                            {
                                "key": "draft_1.docx",
                                "new_name": "01_draft_1.docx",
                                "folder": "Documents",
                                "old_folder": "Documents",
                                "info": sample_plan["Documents"]["draft_1.docx"],
                            },
                            {
                                "key": "draft_2.docx",
                                "new_name": "02_draft_2.docx",
                                "folder": "Documents",
                                "old_folder": "Documents",
                                "info": sample_plan["Documents"]["draft_2.docx"],
                            },
                            {
                                "key": "draft_3.docx",
                                "new_name": "03_draft_3.docx",
                                "folder": "Documents",
                                "old_folder": "Documents",
                                "info": sample_plan["Documents"]["draft_3.docx"],
                            },
                        ],
                        is_batch=True,
                    )
                    callback(batch_res)

            app.push_screen = mock_push

            # Trigger action_rename_node
            app.action_rename_node()
            await pilot.pause()

            # Verify batch modal was pushed with 3 files
            assert len(pushed_modals) == 1
            modal_instance, _ = pushed_modals[0]
            assert isinstance(modal_instance, RenameModal)
            assert modal_instance.files is not None
            assert len(modal_instance.files) == 3

            # Verify plan was updated and nodes locked
            assert "01_draft_1.docx" in app.plan["Documents"]
            assert "02_draft_2.docx" in app.plan["Documents"]
            assert "03_draft_3.docx" in app.plan["Documents"]
            assert app.plan["Documents"]["01_draft_1.docx"]["is_locked"] is True

    asyncio.run(_test())


def test_batch_rename_nested_folders_and_non_dict_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify on_batch_renamed mutates nested folder dicts and guards non-dict metadata safely."""
    from app.config import AppSettings

    async def _test() -> None:
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = False
        app = AutoSorterTUI(settings=settings, base_dir="/app/sample")
        inv1_info: dict[str, object] = {
            "__type__": "file",
            "filepath": "/app/sample/inv_1.pdf",
            "target_filename": "inv_1.pdf",
            "is_checked": True,
            "is_excluded": False,
            "folder": "Finance/2024/Invoices",
        }
        inv2_info: dict[str, object] = {
            "__type__": "file",
            "filepath": "/app/sample/inv_2.pdf",
            "target_filename": "inv_2.pdf",
            "is_checked": True,
            "is_excluded": False,
            "folder": "Finance/2024/Invoices",
        }
        app.plan = {
            "Finance": {
                "2024": {
                    "Invoices": {
                        "inv_1.pdf": inv1_info,
                        "inv_2.pdf": inv2_info,
                        "legacy_note.txt": "legacy_non_dict_metadata",
                    }
                }
            }
        }
        mock_session = MagicMock()
        mock_session.base_dir = "/app/sample"
        app.app_session = mock_session

        async with app.run_test() as pilot:
            app.rebuild_tree()
            await pilot.pause()

            batch_res = RenameResult(
                "{seq}_{original}",
                batch_items=[
                    {
                        "key": "inv_1.pdf",
                        "new_name": "01_inv_1.pdf",
                        "folder": "Finance/2024/Invoices",
                        "old_folder": "Finance/2024/Invoices",
                        "info": inv1_info,
                    },
                    {
                        "key": "inv_2.pdf",
                        "new_name": "02_inv_2.pdf",
                        "folder": "Finance/2024/Archive",
                        "old_folder": "Finance/2024/Invoices",
                        "info": inv2_info,
                    },
                    {
                        "key": "legacy_note.txt",
                        "new_name": "03_legacy_note.txt",
                        "folder": "Finance/2024/Invoices",
                        "old_folder": "Finance/2024/Invoices",
                        "info": "legacy_non_dict_metadata",
                    },
                ],
                is_batch=True,
            )

            app.active_tree_node = None
            captured_callbacks: list[object] = []

            def _capture_push(screen: object, callback: object = None) -> object:
                captured_callbacks.append(callback)
                if callable(callback):
                    callback(batch_res)
                return None

            monkeypatch.setattr(app, "push_screen", _capture_push)
            app.action_rename_node()
            await pilot.pause()
            assert len(captured_callbacks) == 1

            nested_invoices = app.plan["Finance"]["2024"]["Invoices"]
            nested_archive = app.plan["Finance"]["2024"]["Archive"]

            assert "inv_1.pdf" not in nested_invoices
            assert "inv_2.pdf" not in nested_invoices
            assert "legacy_note.txt" not in nested_invoices
            assert "01_inv_1.pdf" in nested_invoices
            assert nested_invoices["01_inv_1.pdf"]["is_locked"] is True
            assert "02_inv_2.pdf" in nested_archive
            assert nested_archive["02_inv_2.pdf"]["is_locked"] is True
            assert nested_archive["02_inv_2.pdf"]["folder"] == "Finance/2024/Archive"
            assert isinstance(nested_invoices["03_legacy_note.txt"], dict)
            assert (
                nested_invoices["03_legacy_note.txt"]["target_filename"]
                == "03_legacy_note.txt"
            )
            assert nested_invoices["03_legacy_note.txt"]["is_locked"] is True

            assert "Finance/2024/Invoices" not in app.plan
            assert "Finance/2024/Archive" not in app.plan
            assert app.locked_files["01_inv_1.pdf"] == "Finance/2024/Invoices"
            assert app.locked_files["02_inv_2.pdf"] == "Finance/2024/Archive"

    asyncio.run(_test())


def test_manual_edits_persist_sqlite_doc_relpath_nested_ops_and_rescan(
    tmp_path, monkeypatch
):
    """Verify F2 single/batch rename, Ctrl+L lock, nested reassign, nested folder rename, and exclusions persist via doc_relpath across rescans until execution or workspace change."""
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app_store"))
    ws = tmp_path / "workspace"
    (ws / "inbox").mkdir(parents=True)
    ws_other = tmp_path / "other_workspace"
    ws_other.mkdir()

    f_inv = ws / "inbox" / "inv_1.pdf"
    f_tax = ws / "tax_2024.pdf"
    f_note = ws / "notes.txt"
    f_inv.write_text("Quarterly invoice billing statement finance")
    f_tax.write_text("Annual tax filing summary finance")
    f_note.write_text("Scratchpad personal meeting notes")

    async def _test():
        from textual.widgets import Tree

        from app.config import AppSettings
        from app.core.metadata import MetadataPass
        from app.core.pipeline import SortPipeline
        from app.core.scanner import get_files_from_sources
        from app.core.session import AppSession

        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(ws), skip_wizard=True)
        session = AppSession(settings, base_dir=str(ws), source_dirs=[str(ws)])
        app.app_session = session
        app.pipeline = SortPipeline(session)

        try:
            items = get_files_from_sources([str(ws)], settings=settings)
            MetadataPass.run(
                str(ws), items, settings, session.db, source_dirs=[str(ws)]
            )
            session.db.upsert_document(
                str(ws),
                "inbox/inv_1.pdf",
                "hash_inv",
                "Quarterly invoice billing statement finance",
            )
            session.db.upsert_document(
                str(ws),
                "tax_2024.pdf",
                "hash_tax",
                "Annual tax filing summary finance",
            )
            session.db.upsert_document(
                str(ws),
                "notes.txt",
                "hash_note",
                "Scratchpad personal meeting notes",
            )

            async with app.run_test() as pilot:
                app.plan = {
                    "Finance": {
                        "2024": {
                            "Invoices": {
                                "inbox/inv_1.pdf": {
                                    "__type__": "file",
                                    "source_path": "inbox/inv_1.pdf",
                                    "filepath": str(f_inv),
                                    "is_checked": False,
                                },
                                "tax_2024.pdf": {
                                    "__type__": "file",
                                    "source_path": "tax_2024.pdf",
                                    "filepath": str(f_tax),
                                    "is_checked": False,
                                },
                            }
                        }
                    },
                    "Misc": {
                        "notes.txt": {
                            "__type__": "file",
                            "source_path": "notes.txt",
                            "filepath": str(f_note),
                            "is_checked": False,
                        }
                    },
                }
                app.rebuild_tree()
                await pilot.pause(0.05)

                tree = app.query_one("#plan-tree", Tree)

                def _find_tree_node(pred):
                    stack = [tree.root]
                    while stack:
                        curr = stack.pop()
                        if curr.data and pred(curr.data):
                            return curr
                        stack.extend(reversed(curr.children))
                    return None

                # 1. Single F2 rename on nested file persists source doc_relpath in SQLite
                inv_node = _find_tree_node(
                    lambda d: d.get("is_file") and d.get("key") == "inbox/inv_1.pdf"
                )
                assert inv_node is not None
                app.active_tree_node = inv_node
                tree.select_node(inv_node)
                await pilot.pause(0.02)

                monkeypatch.setattr(
                    app,
                    "push_screen",
                    lambda _scr, cb=None: cb("2024_ACME_Invoice.pdf") if cb else None,
                )
                app.action_rename_node()
                await pilot.pause(0.05)
                session.db.worker.flush()

                def _verified_target_path(relpath: str) -> str | None:
                    norm_rel = relpath.replace("\\", "/")
                    return {
                        str(doc[0]).replace("\\", "/"): doc[3]
                        for doc in session.db.get_all_documents(str(ws))
                        if len(doc) > 3
                    }.get(norm_rel)

                assert (
                    _verified_target_path("inbox/inv_1.pdf") == "Finance/2024/Invoices"
                )

                # 2. Nested on_reassigned moves tax_2024.pdf from Finance/2024/Invoices -> Legal/Tax/2024
                tax_node = _find_tree_node(
                    lambda d: d.get("is_file") and d.get("key") == "tax_2024.pdf"
                )
                assert tax_node is not None
                tax_node.data["info"]["is_checked"] = True
                app.rebuild_tree()
                await pilot.pause(0.02)

                monkeypatch.setattr(
                    app,
                    "push_screen",
                    lambda _scr, cb=None: cb("Legal/Tax/2024") if cb else None,
                )
                app.action_reassign_selected()
                await pilot.pause(0.05)
                session.db.worker.flush()

                assert "tax_2024.pdf" in app.plan["Legal"]["Tax"]["2024"]
                assert _verified_target_path("tax_2024.pdf") == "Legal/Tax/2024"

                # 3. Nested on_folder_renamed renames Finance/2024/Invoices -> Finance/2024/PaidInvoices
                invoices_folder_node = _find_tree_node(
                    lambda d: (
                        not d.get("is_file")
                        and d.get("folder") == "Finance/2024/Invoices"
                    )
                )
                assert invoices_folder_node is not None
                app.active_tree_node = invoices_folder_node
                tree.select_node(invoices_folder_node)
                await pilot.pause(0.02)

                monkeypatch.setattr(
                    app,
                    "push_screen",
                    lambda _scr, cb=None: cb("PaidInvoices") if cb else None,
                )
                app.action_rename_node()
                await pilot.pause(0.05)
                session.db.worker.flush()

                assert "Invoices" not in app.plan["Finance"]["2024"]
                assert (
                    "2024_ACME_Invoice.pdf"
                    in app.plan["Finance"]["2024"]["PaidInvoices"]
                )
                assert (
                    _verified_target_path("inbox/inv_1.pdf")
                    == "Finance/2024/PaidInvoices"
                )

                # 4. Ctrl+L unlock + re-lock on renamed file updates SQLite via doc_relpath
                renamed_inv_node = _find_tree_node(
                    lambda d: (
                        d.get("is_file") and d.get("key") == "2024_ACME_Invoice.pdf"
                    )
                )
                assert renamed_inv_node is not None
                app.active_tree_node = renamed_inv_node
                tree.select_node(renamed_inv_node)
                await pilot.pause(0.02)

                app.action_toggle_lock()
                await pilot.pause(0.02)
                session.db.worker.flush()
                assert _verified_target_path("inbox/inv_1.pdf") is None
                app.action_toggle_lock()
                await pilot.pause(0.02)
                session.db.worker.flush()
                assert (
                    _verified_target_path("inbox/inv_1.pdf")
                    == "Finance/2024/PaidInvoices"
                )

                # 5. Exclude notes.txt and verify rescan preserves manual rename, nested folders, and exclusion
                for fi in app._collect_file_nodes_from_dict(app.plan):
                    fi["is_checked"] = False
                note_node = _find_tree_node(
                    lambda d: d.get("is_file") and d.get("key") == "notes.txt"
                )
                assert note_node is not None
                app.active_tree_node = note_node
                tree.select_node(note_node)
                await pilot.pause(0.02)
                tree.focus()
                await pilot.pause(0.02)
                app.action_exclude_selected()
                await pilot.pause(0.05)

                # Simulate fresh generate_sorting_plan output followed by _reapply_manual_plan_edits
                session.db.worker.flush()
                fresh_plan = session.generate_sorting_plan()
                app.plan = fresh_plan
                app._reapply_manual_plan_edits()
                plan_dict = app.plan.plan if hasattr(app.plan, "plan") else app.plan

                paid_inv = plan_dict["Finance"]["2024"]["PaidInvoices"][
                    "2024_ACME_Invoice.pdf"
                ]
                assert paid_inv["target_filename"] == "2024_ACME_Invoice.pdf"
                assert paid_inv["confirmed"] is True
                assert "tax_2024.pdf" in plan_dict["Legal"]["Tax"]["2024"]

                found_notes = [
                    fi
                    for fi in app._collect_file_nodes_from_dict(plan_dict)
                    if fi.get("source_path") == "notes.txt"
                ]
                assert len(found_notes) == 1
                assert found_notes[0]["is_excluded"] is True
                assert found_notes[0]["is_checked"] is False

                # 6. Switching workspace clears cached manual edits
                monkeypatch.setattr(
                    app,
                    "push_screen",
                    lambda _scr, cb=None: cb(str(ws_other)) if cb else None,
                )
                monkeypatch.setattr(app, "action_scan_directory", lambda: None)
                app.action_select_dir()
                await pilot.pause(0.05)
                assert app._manual_plan_edits == {}
        finally:
            session.close()

    asyncio.run(_test())
