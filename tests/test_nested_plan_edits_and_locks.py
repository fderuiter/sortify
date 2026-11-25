"""Regression tests for recursive nested plan edits, empty folder persistence, and SQLite filename locks."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, Static, Tree

from app.config import AppSettings
from app.core.db import MAIN_DB_MIGRATIONS
from app.core.migrations import migrate, table_columns
from app.core.mover import AsyncMoveEngine
from app.core.path_utils import (
    collect_empty_plan_folders,
    collect_plan_folder_paths,
    prune_empty_plan_ancestors,
    resolve_plan_folder_dict,
)
from app.core.session import AppSession
from app.core.store import STORE_MIGRATIONS
from app.core.verifier import VerificationEngine
from app.ui.modals.edit import NewFolderModal, ReassignModal, RenameModal
from app.ui.tui import AutoSorterTUI


def _make_settings(tmp_path: Path, **overrides: Any) -> AppSettings:
    defaults: dict[str, Any] = {
        "AI_CONSENT_GRANTED": False,
        "CONTEXTUAL_RENAMING": False,
        "DUPLICATE_ACTION": "off",
        "CONFLICT_POLICY": "rename",
    }
    defaults.update(overrides)
    cfg_file = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(cfg_file))
    for k, v in defaults.items():
        setattr(settings._settings_model, k, v)
    return settings


def _verified_target_path(db: Any, base_dir: str, relpath: str) -> str | None:
    norm_rel = relpath.replace("\\", "/")
    return {
        str(doc[0]).replace("\\", "/"): doc[3]
        for doc in db.get_all_documents(base_dir)
        if len(doc) > 3
    }.get(norm_rel)


def _find_tree_node(
    node: Any,
    *,
    key: str | None = None,
    folder: str | None = None,
    is_file: bool | None = None,
) -> Any:
    data = getattr(node, "data", None)
    if isinstance(data, dict):
        key_ok = key is None or data.get("key") == key
        folder_ok = folder is None or data.get("folder") == folder
        type_ok = is_file is None or bool(data.get("is_file")) is is_file
        if key_ok and folder_ok and type_ok:
            return node
    for child in getattr(node, "children", []):
        found = _find_tree_node(child, key=key, folder=folder, is_file=is_file)
        if found is not None:
            return found
    return None


async def _wait_for_pipeline(app: AutoSorterTUI, pilot: Any) -> None:
    """Await scan/validation/execute workers without waiting on startup session-check worker."""
    await pilot.pause()
    for _ in range(200):
        active = [
            w
            for w in list(app.workers)
            if w.name
            in {"run_scan_worker", "run_execute_worker", "run_validation_worker"}
            and not w.is_finished
        ]
        if not active and app._running_pipeline_worker() is None:
            break
        for w in active:
            await w.wait()
        await pilot.pause(0.05)
    await pilot.pause()


def test_shared_plan_helpers_resolve_collect_and_prune() -> None:
    plan: dict[str, Any] = {
        "Financial": {
            "Invoices": {
                "inv.pdf": {"__type__": "file", "source_path": "inv.pdf"},
            },
            "Audits": {},
        },
        "Taxes": {
            "2025": {
                "Returns": {},
            }
        },
    }

    assert resolve_plan_folder_dict(plan, "") is plan
    assert (
        resolve_plan_folder_dict(plan, "Financial/Invoices")
        is plan["Financial"]["Invoices"]
    )
    assert (
        resolve_plan_folder_dict(plan, "Financial\\Invoices")
        is plan["Financial"]["Invoices"]
    )
    assert resolve_plan_folder_dict(plan, "Missing/Sub", create=False) is None

    created = resolve_plan_folder_dict(plan, "Archive/Legal/Contracts", create=True)
    assert created == {}
    assert plan["Archive"]["Legal"]["Contracts"] is created

    # Never overwrite an existing file entry when resolving a folder path
    assert (
        resolve_plan_folder_dict(plan, "Financial/Invoices/inv.pdf", create=True)
        is None
    )

    folder_paths = collect_plan_folder_paths(plan)
    assert "Financial" in folder_paths
    assert "Financial/Invoices" in folder_paths
    assert "Financial/Audits" in folder_paths
    assert "Taxes/2025/Returns" in folder_paths
    assert "Archive/Legal/Contracts" in folder_paths

    empty_folders = collect_empty_plan_folders(plan)
    assert set(empty_folders) == {
        "Financial/Audits",
        "Taxes/2025/Returns",
        "Archive/Legal/Contracts",
    }

    # Prune empty ancestors while preserving manual folders
    plan["Financial"]["Invoices"].pop("inv.pdf")
    prune_empty_plan_ancestors(
        plan,
        "Financial/Invoices",
        preserve_folders={"Financial/Audits"},
    )
    assert "Invoices" not in plan["Financial"]
    assert "Audits" in plan["Financial"]

    prune_empty_plan_ancestors(plan, "Archive/Legal/Contracts")
    assert "Archive" not in plan


def test_migrations_add_user_verified_target_name_and_preserve_folder_only_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Verify legacy v7 main DB upgrades cleanly to v8
    raw_main = sqlite3.connect(":memory:")
    migrate(raw_main, MAIN_DB_MIGRATIONS[:-1], label="main-v7")
    assert raw_main.execute("PRAGMA user_version").fetchone()[0] == 7
    assert "user_verified_target_name" not in table_columns(raw_main, "documents")
    assert migrate(raw_main, MAIN_DB_MIGRATIONS, label="main-v8") == 8
    assert "user_verified_target_name" in table_columns(raw_main, "documents")
    raw_main.close()

    # 2. Verify legacy v4 store upgrades cleanly to v5 and head version
    raw_store = sqlite3.connect(":memory:")
    migrate(raw_store, STORE_MIGRATIONS[:4], label="store-v4")
    assert raw_store.execute("PRAGMA user_version").fetchone()[0] == 4
    assert "user_verified_target_name" not in table_columns(raw_store, "documents")
    assert migrate(raw_store, STORE_MIGRATIONS[:5], label="store-v5") == 5
    assert "user_verified_target_name" in table_columns(raw_store, "documents")
    assert (
        migrate(raw_store, STORE_MIGRATIONS, label="store-head")
        == STORE_MIGRATIONS[-1].version
    )
    raw_store.close()

    # 3. Verify Database methods keep user_verified_target_path strictly folder-only
    app_dir = tmp_path / "app_data"
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    settings = _make_settings(tmp_path)

    session = AppSession(settings, str(ws))
    try:
        db = session.db
        db.upsert_document(str(ws), "invoice.pdf", "h1", "Acme invoice #101")

        db.set_user_verified_target_path(
            str(ws),
            "invoice.pdf",
            "Financial/Invoices",
            target_name="2025_Acme_Invoice.pdf",
        )
        assert _verified_target_path(db, str(ws), "invoice.pdf") == "Financial/Invoices"
        assert (
            db.get_user_verified_target_name(str(ws), "invoice.pdf")
            == "2025_Acme_Invoice.pdf"
        )

        # Reassigning folder without target_name preserves user_verified_target_name
        db.set_user_verified_target_path(str(ws), "invoice.pdf", "Taxes/2025")
        assert _verified_target_path(db, str(ws), "invoice.pdf") == "Taxes/2025"
        assert (
            db.get_user_verified_target_name(str(ws), "invoice.pdf")
            == "2025_Acme_Invoice.pdf"
        )

        # Root-level rename keeps user_verified_target_path NULL while storing target_name
        db.set_user_verified_target_path(
            str(ws),
            "invoice.pdf",
            "",
            target_name="Root_Renamed.pdf",
        )
        assert _verified_target_path(db, str(ws), "invoice.pdf") is None
        assert (
            db.get_user_verified_target_name(str(ws), "invoice.pdf")
            == "Root_Renamed.pdf"
        )

        # Unlocking clears both target_path and target_name
        db.set_user_verified_target_path(str(ws), "invoice.pdf", None)
        assert _verified_target_path(db, str(ws), "invoice.pdf") is None
        assert db.get_user_verified_target_name(str(ws), "invoice.pdf") is None

        # Moving file on disk clears user_verified_target_name so rescan won't double-rename
        db.set_user_verified_target_path(
            str(ws),
            "invoice.pdf",
            "Taxes",
            target_name="Moved_Invoice.pdf",
        )
        db.update_document_path(
            str(ws), "invoice.pdf", str(ws), "Taxes/Moved_Invoice.pdf"
        )
        assert (
            db.get_user_verified_target_name(str(ws), "Taxes/Moved_Invoice.pdf") is None
        )
    finally:
        session.close()


def test_nested_reassign_and_folder_rename_update_tree_db_and_avoid_false_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_dir = tmp_path / "app_nested"
    ws = tmp_path / "ws_nested"
    ws.mkdir()
    inv_path = ws / "inv_01.pdf"
    ret_path = ws / "return_2024.pdf"
    inv_path.write_text("Acme consulting invoice #01 for Q1 services", encoding="utf-8")
    ret_path.write_text("Federal tax return 2024 filing summary", encoding="utf-8")

    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    settings = _make_settings(tmp_path)

    async def _run() -> None:
        session = AppSession(settings, str(ws))
        session.db.upsert_document(str(ws), "inv_01.pdf", "h_inv", "invoice")
        session.db.upsert_document(str(ws), "return_2024.pdf", "h_ret", "tax return")

        app = AutoSorterTUI(
            base_dir=str(ws),
            settings=settings,
            skip_wizard=True,
        )
        app.app_session = session
        app.plan = {
            "Financial": {
                "Invoices": {
                    "inv_01.pdf": {
                        "__type__": "file",
                        "source_path": "inv_01.pdf",
                        "source_root": str(ws),
                        "filepath": str(inv_path),
                        "relative_source": "../../inv_01.pdf",
                        "folder": "Financial/Invoices",
                        "is_checked": True,
                    }
                }
            },
            "Taxes": {
                "Returns": {
                    "return_2024.pdf": {
                        "__type__": "file",
                        "source_path": "return_2024.pdf",
                        "source_root": str(ws),
                        "filepath": str(ret_path),
                        "relative_source": "../../return_2024.pdf",
                        "folder": "Taxes/Returns",
                        "is_checked": False,
                    }
                }
            },
        }

        try:
            async with app.run_test(size=(120, 40)) as pilot:
                app.rebuild_tree(run_validation=False)
                await pilot.pause()

                # Trigger [R] Reassign on checked inv_01.pdf
                app.action_reassign_selected()
                await pilot.pause()
                assert isinstance(app.screen, ReassignModal)
                modal = app.screen

                # Nested folders must be enumerated in existing_folders
                assert "Financial/Invoices" in modal.existing_folders
                assert "Taxes/Returns" in modal.existing_folders

                inp = modal.query_one("#input-target-folder", Input)
                inp.value = "Taxes/Returns"
                await pilot.pause()
                modal.action_confirm()
                await pilot.pause()

                # Relocated into nested container without creating a flat "Taxes/Returns" key
                assert "Taxes/Returns" not in app.plan
                assert "Financial" not in app.plan
                assert "inv_01.pdf" in app.plan["Taxes"]["Returns"]
                assert "return_2024.pdf" in app.plan["Taxes"]["Returns"]
                assert (
                    _verified_target_path(session.db, str(ws), "inv_01.pdf")
                    == "Taxes/Returns"
                )

                # Zero false self-collisions in VerificationEngine.get_duplicate_groups
                norm_plan = app._normalize_plan_for_verification(
                    app._get_executable_plan(), str(ws)
                )
                dup_groups = VerificationEngine.get_duplicate_groups(str(ws), norm_plan)
                assert dup_groups == []

                # Now focus nested folder "Taxes/Returns" and rename via [F2] to "Archive/TaxFilings"
                tree = app.query_one("#plan-tree", Tree)
                returns_node = _find_tree_node(
                    tree.root, folder="Taxes/Returns", is_file=False
                )
                assert returns_node is not None
                tree.move_cursor(returns_node)
                app.active_tree_node = returns_node
                await pilot.pause()

                app.action_rename_node()
                await pilot.pause()
                assert isinstance(app.screen, RenameModal)
                rename_modal = app.screen
                rename_inp = rename_modal.query_one("#input-name", Input)
                rename_inp.value = "Archive/TaxFilings"
                await pilot.pause()
                rename_modal.action_confirm()
                await pilot.pause()

                assert "Taxes" not in app.plan
                assert "inv_01.pdf" in app.plan["Archive"]["TaxFilings"]
                assert "return_2024.pdf" in app.plan["Archive"]["TaxFilings"]
                assert (
                    _verified_target_path(session.db, str(ws), "inv_01.pdf")
                    == "Archive/TaxFilings"
                )
                assert (
                    _verified_target_path(session.db, str(ws), "return_2024.pdf")
                    == "Archive/TaxFilings"
                )
        finally:
            session.close()

    asyncio.run(_run())


def test_rename_and_reassign_modals_detect_nested_plan_collisions_and_traversal(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws_collisions"
    ws.mkdir()
    f1 = ws / "draft.pdf"
    f2 = ws / "existing.pdf"
    f1.write_text("draft content", encoding="utf-8")
    f2.write_text("existing content", encoding="utf-8")

    nested_plan: dict[str, Any] = {
        "Financial": {
            "Invoices": {
                "draft.pdf": {
                    "__type__": "file",
                    "source_path": "draft.pdf",
                    "filepath": str(f1),
                    "folder": "Financial/Invoices",
                },
                "existing.pdf": {
                    "__type__": "file",
                    "source_path": "existing.pdf",
                    "filepath": str(f2),
                    "folder": "Financial/Invoices",
                },
            }
        },
        "Taxes": {
            "Returns": {
                "draft.pdf": {
                    "__type__": "file",
                    "source_path": "other_draft.pdf",
                    "filepath": str(ws / "other_draft.pdf"),
                    "folder": "Taxes/Returns",
                }
            }
        },
    }

    async def _run() -> None:
        app = AutoSorterTUI(
            base_dir=str(ws),
            settings=_make_settings(tmp_path),
            skip_wizard=True,
        )
        app.plan = nested_plan
        async with app.run_test(size=(120, 40)) as pilot:
            # 1. Single-file RenameModal inside nested folder "Financial/Invoices"
            rename_modal = RenameModal(
                title="Rename File: draft.pdf",
                current_name="draft",
                extension=".pdf",
                category="Financial/Invoices",
                is_folder=False,
                plan=nested_plan,
                base_dir=str(ws),
                source_path=str(f1),
            )
            app.push_screen(rename_modal)
            await pilot.pause()

            inp = rename_modal.query_one("#input-name", Input)
            warn = rename_modal.query_one("#warning-collision", Static)

            inp.value = "existing"
            await pilot.pause()
            warn_text = str(warn.render())
            assert "Collision detected" in warn_text
            assert "existing.pdf" in warn_text
            assert "Financial/Invoices" in warn_text

            # Traversal destination override blocks confirmation
            dest_inp = rename_modal.query_one("#input-destination", Input)
            dest_inp.value = "../escape"
            await pilot.pause()
            assert warn.has_class("modal-error")
            assert "Directory traversal" in str(warn.render())
            rename_modal.action_confirm()
            await pilot.pause()
            assert app.screen is rename_modal
            rename_modal.dismiss(None)
            await pilot.pause()

            # 2. Folder RenameModal merging nested folder into existing "Taxes/Returns"
            folder_modal = RenameModal(
                title="Rename Folder: Financial/Invoices",
                current_name="Invoices",
                category="Financial/Invoices",
                is_folder=True,
                plan=nested_plan,
                base_dir=str(ws),
            )
            app.push_screen(folder_modal)
            await pilot.pause()
            f_inp = folder_modal.query_one("#input-name", Input)
            f_warn = folder_modal.query_one("#warning-collision", Static)
            f_inp.value = "Taxes/Returns"
            await pilot.pause()
            f_warn_text = str(f_warn.render())
            assert "Taxes/Returns" in f_warn_text
            assert "merge" in f_warn_text.lower()
            folder_modal.dismiss(None)
            await pilot.pause()

            # 3. ReassignModal colliding with "draft.pdf" inside nested "Taxes/Returns"
            reassign_modal = ReassignModal(
                ["Financial/Invoices", "Taxes/Returns"],
                count=1,
                plan=nested_plan,
                base_dir=str(ws),
                target_files=[nested_plan["Financial"]["Invoices"]["draft.pdf"]],
            )
            app.push_screen(reassign_modal)
            await pilot.pause()
            r_inp = reassign_modal.query_one("#input-target-folder", Input)
            r_warn = reassign_modal.query_one("#warning-collision", Static)
            r_inp.value = "Taxes/Returns"
            await pilot.pause()
            r_warn_text = str(r_warn.render())
            assert "Collision detected" in r_warn_text
            assert "draft.pdf" in r_warn_text
            assert "Taxes/Returns" in r_warn_text
            reassign_modal.dismiss(None)
            await pilot.pause()

    asyncio.run(_run())


def test_f2_single_and_batch_renames_persist_in_sqlite_across_fresh_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_dir = tmp_path / "app_f2_persist"
    ws = tmp_path / "ws_f2_persist"
    ws.mkdir()
    inv_file = ws / "scan_001.txt"
    rec_file = ws / "scan_002.txt"
    inv_file.write_text("Invoice #9001 Acme Corp total $500 due", encoding="utf-8")
    rec_file.write_text("Payment receipt #9002 Beta LLC paid in full", encoding="utf-8")

    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    # Enable CONTEXTUAL_RENAMING on restart to verify FileRenamerEngine never clobbers SQLite locks
    settings = _make_settings(
        tmp_path,
        CONTEXTUAL_RENAMING=True,
        KEYWORD_RULES={
            "invoice": "Financial/Invoices",
            "receipt": "Financial/Invoices",
        },
    )

    expected_batch_names: set[str] = set()

    async def _session_one_rename() -> None:
        app = AutoSorterTUI(
            base_dir=str(ws),
            settings=settings,
            skip_wizard=True,
        )
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                app.action_scan_directory()
                await _wait_for_pipeline(app, pilot)

                # Deselect all first so single-file F2 rename triggers instead of batch rename
                for fi in app._collect_file_nodes_from_dict(app.plan):
                    fi["is_checked"] = False
                app.rebuild_tree(run_validation=False)
                await pilot.pause()

                # Locate scan_001.txt by source_path in tree
                tree = app.query_one("#plan-tree", Tree)
                target_node = None
                for fi in app._collect_file_nodes_from_dict(app.plan):
                    if fi.get("source_path") == "scan_001.txt":
                        found_node = _find_tree_node(
                            tree.root, folder=fi.get("folder"), is_file=True
                        )

                        # Match exact info dict
                        def _match_info(n: Any, expected: Any = fi) -> Any:
                            d = getattr(n, "data", None)
                            if isinstance(d, dict) and d.get("info") is expected:
                                return n
                            for ch in getattr(n, "children", []):
                                r = _match_info(ch, expected)
                                if r is not None:
                                    return r
                            return None

                        target_node = _match_info(tree.root) or found_node
                        break

                assert target_node is not None
                tree.move_cursor(target_node)
                app.active_tree_node = target_node
                await pilot.pause()

                # Single-file F2 rename
                app.action_rename_node()
                await pilot.pause()
                assert isinstance(app.screen, RenameModal)
                modal = app.screen
                inp = modal.query_one("#input-name", Input)
                inp.value = "2025_Q1_Acme_Invoice"
                await pilot.pause()
                modal.action_confirm()
                await pilot.pause()

                assert app.app_session is not None
                stored_folder = _verified_target_path(
                    app.app_session.db, str(ws), "scan_001.txt"
                )
                stored_name = app.app_session.db.get_user_verified_target_name(
                    str(ws), "scan_001.txt"
                )
                assert stored_folder == "Financial/Invoices"
                assert stored_name == "2025_Q1_Acme_Invoice.txt"
                assert "2025_Q1_Acme_Invoice" not in (stored_folder or "")

                # Now check both files and perform a batch F2 rename on scan_002.txt + scan_001.txt
                # First test rescan inside same TUI session
                app.action_scan_directory()
                await _wait_for_pipeline(app, pilot)
                inv_container = resolve_plan_folder_dict(app.plan, "Financial/Invoices")
                assert inv_container is not None
                assert "2025_Q1_Acme_Invoice.txt" in inv_container
                assert (
                    inv_container["2025_Q1_Acme_Invoice.txt"]["target_filename"]
                    == "2025_Q1_Acme_Invoice.txt"
                )

                # Now check all files and perform a batch F2 rename with destination override
                for fi in app._collect_file_nodes_from_dict(app.plan):
                    fi["is_checked"] = True
                app.rebuild_tree(run_validation=False)
                await pilot.pause()
                tree = app.query_one("#plan-tree", Tree)
                first_file_node = _find_tree_node(tree.root, is_file=True)
                assert first_file_node is not None
                tree.move_cursor(first_file_node)
                app.active_tree_node = first_file_node
                await pilot.pause()

                app.action_rename_node()
                await pilot.pause()
                assert isinstance(app.screen, RenameModal)
                batch_modal = app.screen
                assert bool(batch_modal.files) is True
                expected_batch_names.update(
                    {
                        f"Batch_{batch_modal.seq:02d}.txt",
                        f"Batch_{batch_modal.seq + 1:02d}.txt",
                    }
                )
                batch_inp = batch_modal.query_one("#input-name", Input)
                batch_dest = batch_modal.query_one("#input-destination", Input)
                batch_inp.value = "Batch_{seq}"
                batch_dest.value = "Archive/2025/Invoices"
                await pilot.pause()
                batch_modal.action_confirm()
                await pilot.pause()

                assert (
                    _verified_target_path(app.app_session.db, str(ws), "scan_001.txt")
                    == "Archive/2025/Invoices"
                )
                assert (
                    _verified_target_path(app.app_session.db, str(ws), "scan_002.txt")
                    == "Archive/2025/Invoices"
                )
                name_1 = app.app_session.db.get_user_verified_target_name(
                    str(ws), "scan_001.txt"
                )
                name_2 = app.app_session.db.get_user_verified_target_name(
                    str(ws), "scan_002.txt"
                )
                assert name_1 in expected_batch_names
                assert name_2 in expected_batch_names
                assert name_1 != name_2
        finally:
            if app.app_session:
                app.app_session.close()

    asyncio.run(_session_one_rename())

    # Brand-new AppSession (simulating full app restart with empty _manual_plan_edits!)
    fresh_session = AppSession(settings, str(ws))
    try:
        fresh_plan = fresh_session.generate_sorting_plan()
        inv_container = resolve_plan_folder_dict(fresh_plan, "Archive/2025/Invoices")
        assert inv_container is not None
        for batch_fn in expected_batch_names:
            assert batch_fn in inv_container
            restored_node = inv_container[batch_fn]
            assert restored_node["target_filename"] == batch_fn
            assert restored_node["confirmed"] is True
            assert restored_node["is_locked"] is True
            assert restored_node["source_path"] in {"scan_001.txt", "scan_002.txt"}
        # Verify folder column in SQLite was never polluted with filename
        assert (
            _verified_target_path(fresh_session.db, str(ws), "scan_001.txt")
            == "Archive/2025/Invoices"
        )
    finally:
        fresh_session.close()


def test_ctrl_n_nested_empty_folder_validation_cache_persistence_and_ctrl_e_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_dir = tmp_path / "app_manual_folders"
    ws = tmp_path / "ws_empty"
    ws.mkdir()
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    settings = _make_settings(tmp_path)

    async def _create_in_first_session() -> None:
        app = AutoSorterTUI(
            base_dir=str(ws),
            settings=settings,
            skip_wizard=True,
        )
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                app.action_scan_directory()
                await _wait_for_pipeline(app, pilot)

                # Open Ctrl+N modal
                app.action_new_folder()
                await pilot.pause()
                assert isinstance(app.screen, NewFolderModal)
                modal = app.screen
                inp = modal.query_one("#input-folder-name", Input)
                warn = modal.query_one("#warning-collision", Static)

                # Invalid traversal path is blocked inline
                inp.value = "../outside"
                await pilot.pause()
                assert warn.has_class("modal-error")
                assert (
                    ".." in str(warn.render())
                    or "traversal" in str(warn.render()).lower()
                )
                modal.action_confirm()
                await pilot.pause()
                assert app.screen is modal

                # Valid slash-separated nested folder path
                inp.value = "Financial/Audits/2025"
                await pilot.pause()
                assert str(warn.render()) == ""
                modal.action_confirm()
                await pilot.pause()

                # Nested dicts created (no flat "Financial/Audits/2025" key)
                assert "Financial/Audits/2025" not in app.plan
                assert app.plan["Financial"]["Audits"]["2025"] == {}

                # Rendered in TreeViewMixin even when workspace has 0 files
                tree = app.query_one("#plan-tree", Tree)
                leaf_folder_node = _find_tree_node(
                    tree.root, folder="Financial/Audits/2025", is_file=False
                )
                assert leaf_folder_node is not None

                # Persisted in CacheManager
                assert app.app_session is not None
                cached_manual = app.app_session.cache_manager.load_manual_folders(
                    str(ws)
                )
                assert "Financial/Audits/2025" in cached_manual
        finally:
            if app.app_session:
                app.app_session.close()

    asyncio.run(_create_in_first_session())

    # Restart a brand-new TUI session, rescan empty workspace, and execute [Ctrl+E]
    async def _rescan_and_execute_in_second_session() -> None:
        app2 = AutoSorterTUI(
            base_dir=str(ws),
            settings=settings,
            skip_wizard=True,
        )
        try:
            async with app2.run_test(size=(120, 40)) as pilot:
                app2.action_scan_directory()
                await _wait_for_pipeline(app2, pilot)

                # Restored from CacheManager into nested plan and tree
                assert (
                    app2.plan.get("Financial", {}).get("Audits", {}).get("2025") == {}
                )
                tree = app2.query_one("#plan-tree", Tree)
                assert (
                    _find_tree_node(
                        tree.root, folder="Financial/Audits/2025", is_file=False
                    )
                    is not None
                )

                exec_plan = app2._get_executable_plan()
                assert exec_plan == {"Financial": {"Audits": {"2025": {}}}}

                # Execute plan via Ctrl+E -> materializes empty folder on disk
                app2.action_execute_sort()
                await _wait_for_pipeline(app2, pilot)

                assert (ws / "Financial" / "Audits" / "2025").is_dir()
        finally:
            if app2.app_session:
                app2.app_session.close()

    asyncio.run(_rescan_and_execute_in_second_session())


def test_async_move_engine_blocks_escaping_empty_folder(tmp_path: Path) -> None:
    ws = tmp_path / "ws_sec"
    ws.mkdir()
    engine = AsyncMoveEngine()
    with pytest.raises(ValueError):
        engine.execute(str(ws), {"../escaped_dir": {}}, None, None)
    assert not (tmp_path / "escaped_dir").exists()
