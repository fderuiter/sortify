"""End-to-end regression tests for TUI folder replacement, multi-source :: keys, recoverable collisions, overwrite serialization, and workspace-scoped undo."""

import asyncio
import hashlib
import os
import shutil
from unittest.mock import patch

import pytest
from textual.widgets import Button, Input, Static

from app.config import AppSettings
from app.core.db_conn import get_db_connection
from app.core.db_worker import DBWorker
from app.core.metadata import MetadataPass
from app.core.mover import OVERWRITE_BACKUP_DIR, execute_moves, overwrite_backup_dir
from app.core.scanner import get_files_from_sources
from app.core.session import AppSession
from app.core.store import open_store
from app.core.verifier import VerificationEngine
from app.ui.modals.edit import (
    DirectorySelectModal,
    InteractiveCollisionResolverModal,
)
from app.ui.tui import AutoSorterTUI, RollbackConfirmModal

pytestmark = pytest.mark.xdist_group(name="tui")


def test_directory_select_modal_replaces_sources_by_default_and_clears_sources(
    tmp_path, monkeypatch
):
    """Verify Ctrl+B replaces selected_sources on confirm, presets, and Native Browse unless Add Path was clicked, and Clear Sources resets staged roots."""
    dir_a = tmp_path / "dir_a"
    dir_b = tmp_path / "dir_b"
    dir_c = tmp_path / "dir_c"
    dir_d = tmp_path / "dir_d"
    fake_downloads = tmp_path / "Downloads"
    for d in (dir_a, dir_b, dir_c, dir_d, fake_downloads):
        d.mkdir()

    orig_expanduser = os.path.expanduser
    monkeypatch.setattr(
        os.path,
        "expanduser",
        lambda p: str(fake_downloads) if p == "~/Downloads" else orig_expanduser(p),
    )

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(
            settings=settings,
            base_dir=str(dir_a),
            source_dirs=[str(dir_a), str(dir_b)],
            skip_wizard=True,
        )
        scans: list[list[str]] = []
        app.action_scan_directory = lambda: scans.append(list(app.source_dirs))

        async with app.run_test() as pilot:
            # 1. Opening modal with 2 existing sources, typing dir_c and pressing Select replaces sources
            await pilot.press("ctrl+b")
            await pilot.pause(0.05)
            modal = app.screen
            assert isinstance(modal, DirectorySelectModal)
            assert len(modal.selected_sources) == 2

            inp = modal.query_one("#input-dir", Input)
            inp.value = str(dir_c)
            modal.query_one("#btn-confirm", Button).press()
            await pilot.pause(0.05)

            assert app.base_dir == str(dir_c)
            assert app.source_dirs == [str(dir_c)]
            assert scans[-1] == [str(dir_c)]

            # 2. Native Browse without Add Path replaces selected_sources instead of appending
            await pilot.press("ctrl+b")
            await pilot.pause(0.05)
            modal = app.screen
            assert isinstance(modal, DirectorySelectModal)
            modal._on_native_dir_selected(str(dir_d))
            await pilot.pause(0.05)
            assert modal.selected_sources == [str(dir_d)]
            modal.query_one("#btn-confirm", Button).press()
            await pilot.pause(0.05)
            assert app.base_dir == str(dir_d)
            assert app.source_dirs == [str(dir_d)]

            # 3. Clear Sources empties staged sources; Add Path opts into multi-source mode
            await pilot.press("ctrl+b")
            await pilot.pause(0.05)
            modal = app.screen
            assert isinstance(modal, DirectorySelectModal)
            btn_clear = modal.query_one("#btn-clear-sources", Button)
            assert btn_clear.tooltip is not None
            btn_clear.press()
            await pilot.pause(0.05)
            assert modal.selected_sources == []
            assert "(No source directories selected)" in str(
                modal.query_one("#static-sources-list", Static).render()
            )

            inp = modal.query_one("#input-dir", Input)
            inp.value = str(dir_a)
            modal.query_one("#btn-add-dir", Button).press()
            await pilot.pause(0.05)
            modal._on_native_dir_selected(str(dir_b))
            await pilot.pause(0.05)
            modal.query_one("#btn-confirm", Button).press()
            await pilot.pause(0.05)
            assert app.source_dirs == [str(dir_a), str(dir_b)]
            assert app.base_dir == str(dir_a)

            # 4. Preset button replaces multi-source list with single preset root
            await pilot.press("ctrl+b")
            await pilot.pause(0.05)
            modal = app.screen
            assert isinstance(modal, DirectorySelectModal)
            modal.query_one("#preset-downloads", Button).press()
            await pilot.pause(0.05)
            assert app.source_dirs == [str(fake_downloads)]
            assert app.base_dir == str(fake_downloads)

    asyncio.run(_test())


def test_multi_source_preserve_hierarchy_verifier_and_mover_strip_namespace(tmp_path):
    """Verify multi-source <src_root>::<rel_path> keys produce no Unnamed_safe folders or illegal_os_characters errors and move cleanly."""
    root1 = tmp_path / "source_one"
    root2 = tmp_path / "source_two"
    (root1 / "sub_a").mkdir(parents=True)
    (root2 / "sub_b").mkdir(parents=True)

    f1 = root1 / "top_invoice.txt"
    f2 = root1 / "sub_a" / "nested_tax.txt"
    f3 = root2 / "top_notes.txt"
    f4 = root2 / "sub_b" / "nested_agenda.txt"

    f1.write_text("Quarterly invoice payment due")
    f2.write_text("Annual tax return filing summary")
    f3.write_text("Engineering sprint retrospective notes")
    f4.write_text("Architecture review meeting agenda")

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings._settings_model.PRESERVE_HIERARCHY = True
    settings._settings_model.SCAN_MAX_DEPTH = 5

    session = AppSession(settings, source_dirs=[str(root1), str(root2)])
    assert session.base_dir is not None
    try:
        items = get_files_from_sources([str(root1), str(root2)], settings=settings)
        assert len(items) == 4
        assert all("::" in item for item in items)

        MetadataPass.run(
            session.base_dir,
            items,
            settings,
            session.db,
            source_dirs=session.source_dirs,
        )
        session.db.upsert_document(
            str(root1), "top_invoice.txt", "h1", "Quarterly invoice payment due finance"
        )
        session.db.upsert_document(
            str(root1),
            "sub_a/nested_tax.txt",
            "h2",
            "Annual tax return filing summary finance",
        )
        session.db.upsert_document(
            str(root2),
            "top_notes.txt",
            "h3",
            "Engineering sprint retrospective notes tech",
        )
        session.db.upsert_document(
            str(root2),
            "sub_b/nested_agenda.txt",
            "h4",
            "Architecture review meeting agenda tech",
        )

        # Direct unit check on _inject_hierarchy with multi-source keys
        raw_cat_node = {
            f"{root1}::top_invoice.txt": {"__type__": "file"},
            f"{root1}::sub_a/nested_tax.txt": {"__type__": "file"},
            f"{root2}::sub_b/nested_agenda.txt": {"__type__": "file"},
        }
        injected = session.analyzer._inject_hierarchy({"Documents": raw_cat_node})
        docs_node = injected["Documents"]
        assert "Unnamed_safe" not in str(injected)
        assert f"{root1}::top_invoice.txt" in docs_node
        assert "sub_a" in docs_node
        assert f"{root1}::sub_a/nested_tax.txt" in docs_node["sub_a"]
        assert "sub_b" in docs_node
        assert f"{root2}::sub_b/nested_agenda.txt" in docs_node["sub_b"]

        plan = session.generate_sorting_plan()
        plan_dict = plan.plan if hasattr(plan, "plan") else plan
        assert "Unnamed_safe" not in str(plan_dict)

        verification = VerificationEngine.verify_plan_integrity(
            session.base_dir, plan_dict, conflict_policy="rename"
        )
        assert verification["invalid_renames"] == []
        assert verification["has_critical_errors"] is False

        moves_list = VerificationEngine.get_moves(session.base_dir, plan_dict)
        assert len(moves_list) == 4
        for _, src_p, dst_p in moves_list:
            assert "::" not in src_p
            assert "::" not in os.path.basename(dst_p)
            assert os.path.exists(src_p)

        summary = session.execute_moves(plan)
        assert summary.get("cancelled") is False
        for _, _, dst_p in moves_list:
            assert os.path.exists(dst_p)
    finally:
        session.close()


def test_recoverable_collisions_keep_execute_enabled_and_open_resolver_modal(tmp_path):
    """Verify target_collision and duplicate_target stay non-critical, keep #btn-execute enabled, and open InteractiveCollisionResolverModal on Ctrl+E."""
    src1 = tmp_path / "draft1.txt"
    src2 = tmp_path / "draft2.txt"
    src1.write_text("alpha")
    src2.write_text("beta")

    dest_dir = tmp_path / "Finance"
    dest_dir.mkdir()
    existing_target = dest_dir / "existing.txt"
    existing_target.write_text("already here")

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        settings._settings_model.AI_CONSENT_GRANTED = True
        settings._settings_model.CONFLICT_POLICY = "rename"
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path), skip_wizard=True)

        session = AppSession(settings, base_dir=str(tmp_path))
        app.app_session = session
        try:
            async with app.run_test() as pilot:
                app.plan = {
                    "Finance": {
                        "draft1.txt": {
                            "__type__": "file",
                            "relative_source": "draft1.txt",
                            "filepath": str(src1),
                            "target_filename": "existing.txt",
                            "confirmed": True,
                        },
                        "draft2.txt": {
                            "__type__": "file",
                            "relative_source": "draft2.txt",
                            "filepath": str(src2),
                            "target_filename": "existing.txt",
                            "confirmed": True,
                        },
                    }
                }
                app.rebuild_tree()
                await app.workers.wait_for_complete()
                await pilot.pause(0.05)

                res = app.verification_result
                rec_types = {c["type"] for c in res.get("recoverable_collisions", [])}
                assert "target_collision" in rec_types
                assert "duplicate_target" in rec_types
                assert app.has_critical_errors is False

                btn_exec = app.query_one("#btn-execute", Button)
                assert btn_exec.disabled is False

                banner = app.query_one("#alert-banner", Static)
                assert banner.display is True
                assert "alert-warning" in banner.classes
                assert "alert-critical" not in banner.classes

                # Pressing Ctrl+E opens InteractiveCollisionResolverModal
                await pilot.press("ctrl+e")
                await pilot.pause(0.05)
                assert isinstance(app.screen, InteractiveCollisionResolverModal)

                # Cancel modal via Escape to inspect structural critical error contrast
                await pilot.press("escape")
                await pilot.pause(0.05)

                # Structural parent_directory_collision still blocks #btn-execute
                blocking_file = tmp_path / "BlockedAsFile"
                blocking_file.write_text("i am a file blocking a folder")
                app.plan = {
                    "BlockedAsFile": {
                        "SubFolder": {
                            "draft1.txt": {
                                "__type__": "file",
                                "relative_source": "draft1.txt",
                                "filepath": str(src1),
                                "target_filename": "draft1.txt",
                                "confirmed": True,
                            }
                        }
                    }
                }
                app.rebuild_tree()
                await app.workers.wait_for_complete()
                await pilot.pause(0.05)
                assert app.has_critical_errors is True
                assert btn_exec.disabled is True
                assert "alert-critical" in banner.classes
        finally:
            session.close()

    asyncio.run(_test())


def test_per_node_overwrite_displaces_to_backup_and_serializes_concurrent_same_target_moves(
    tmp_path, monkeypatch
):
    """Verify per-node overwrite overrides global rename policy and serializes concurrent moves targeting the same destination without data loss."""
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app_store"))
    root1 = tmp_path / "ws_main"
    root2 = tmp_path / "ws_secondary"
    root1.mkdir()
    root2.mkdir()

    dest_dir = root1 / "Merged"
    dest_dir.mkdir()
    existing_dest = dest_dir / "report.txt"
    existing_dest.write_text("00_EXISTING_ON_DISK")

    src_a = root1 / "report.txt"
    src_b = root2 / "report.txt"
    src_a.write_text("01_FROM_ROOT_ONE")
    src_b.write_text("02_FROM_ROOT_TWO")

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings._settings_model.CONFLICT_POLICY = "rename"
    settings._settings_model.MAX_WORKERS = 4

    store = open_store(tmp_path / "app_store", legacy_session_root=tmp_path / "none")
    worker = DBWorker()
    try:
        db = store.database(worker)
        history_mgr = store.history(db, store.cache(worker))

        key_a = f"{root1}::report.txt"
        key_b = f"{root2}::report.txt"
        plan = {
            "Merged": {
                key_a: {
                    "__type__": "file",
                    "source_path": "report.txt",
                    "relative_source": "../report.txt",
                    "source_root": str(root1),
                    "is_checked": True,
                },
                key_b: {
                    "__type__": "file",
                    "source_path": "report.txt",
                    "relative_source": str(src_b),
                    "source_root": str(root2),
                    "is_checked": True,
                },
            }
        }

        groups = VerificationEngine.get_duplicate_groups(str(root1), plan)
        assert len(groups) == 1
        gid = groups[0]["group_id"]

        VerificationEngine.apply_deduplication_selections(
            str(root1), plan, {gid: "overwrite"}
        )
        assert plan["Merged"][key_a]["overwrite"] is True
        assert plan["Merged"][key_a]["allow_overwrite"] is True
        assert plan["Merged"][key_b]["overwrite"] is True
        assert plan["Merged"][key_b]["allow_overwrite"] is True

        result = execute_moves(
            str(root1),
            plan,
            db=db,
            runtime_settings=settings,
            history_manager=history_mgr,
            max_workers=4,
        )
        session_id = result["session_id"]
        assert session_id

        # No _1 suffixed rename was created; target path holds the final winner
        assert existing_dest.exists()
        assert not (dest_dir / "report_1.txt").exists()
        assert not src_a.exists()
        assert not src_b.exists()

        final_content = existing_dest.read_text()
        assert final_content in ("01_FROM_ROOT_ONE", "02_FROM_ROOT_TWO")

        # Both displaced files (pre-existing disk file + earlier source move) are preserved in session backup!
        bdir = overwrite_backup_dir(str(root1), session_id)
        assert os.path.isdir(bdir)
        backed_up_contents = set()
        for dirpath, _, filenames in os.walk(bdir):
            for fn in filenames:
                with open(os.path.join(dirpath, fn), encoding="utf-8") as fh:
                    backed_up_contents.add(fh.read())

        all_preserved = backed_up_contents | {final_content}
        assert all_preserved == {
            "00_EXISTING_ON_DISK",
            "01_FROM_ROOT_ONE",
            "02_FROM_ROOT_TWO",
        }
        assert (root1 / OVERWRITE_BACKUP_DIR).exists()

        # Rolling back restores all 3 files byte-for-byte
        conn = get_db_connection(history_mgr.db_path)
        with conn:
            conn.execute(
                "UPDATE sessions SET status = 'completed' WHERE session_id = ?",
                (session_id,),
            )
        history_mgr.rollback(session_id)
        assert existing_dest.read_text() == "00_EXISTING_ON_DISK"
        assert src_a.read_text() == "01_FROM_ROOT_ONE"
        assert src_b.read_text() == "02_FROM_ROOT_TWO"
    finally:
        worker.stop()


def test_undo_last_sort_filters_by_active_workspace_and_auto_rescans(
    tmp_path, monkeypatch
):
    """Verify Ctrl+Z selects the latest completed session for self.base_dir (ignoring newer sessions from other workspaces) and rescans after rollback."""
    ws_a = tmp_path / "workspace_a"
    ws_b = tmp_path / "workspace_b"
    ws_c = tmp_path / "workspace_c"
    for w in (ws_a, ws_b, ws_c):
        w.mkdir()

    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app_data"))
    monkeypatch.setattr(
        "app.core.session_catalog.find_abandoned_sessions", lambda *a, **k: []
    )

    store = open_store(tmp_path / "app_data", legacy_session_root=tmp_path / "none")
    worker = DBWorker()
    try:
        db = store.database(worker)
        history_mgr = store.history(db, store.cache(worker))

        # Session A (older) in ws_a
        file_a = ws_a / "alpha.txt"
        file_a.write_text("alpha content")
        hash_a = hashlib.sha256(file_a.read_bytes()).hexdigest()
        sess_a = history_mgr.start_session(str(ws_a))
        sorted_a = ws_a / "Sorted"
        sorted_a.mkdir()
        dst_a = sorted_a / "alpha.txt"
        seq_a = history_mgr.moves.record_intent(
            sess_a, str(file_a), str(dst_a), src_sha256=hash_a
        )
        shutil.move(str(file_a), str(dst_a))
        history_mgr.moves.mark_done(sess_a, seq_a, str(dst_a))

        # Session B (newer) in ws_b
        file_b = ws_b / "beta.txt"
        file_b.write_text("beta content")
        hash_b = hashlib.sha256(file_b.read_bytes()).hexdigest()
        sess_b = history_mgr.start_session(str(ws_b))
        sorted_b = ws_b / "Sorted"
        sorted_b.mkdir()
        dst_b = sorted_b / "beta.txt"
        seq_b = history_mgr.moves.record_intent(
            sess_b, str(file_b), str(dst_b), src_sha256=hash_b
        )
        shutil.move(str(file_b), str(dst_b))
        history_mgr.moves.mark_done(sess_b, seq_b, str(dst_b))

        conn = get_db_connection(history_mgr.db_path)
        with conn:
            conn.execute(
                "UPDATE sessions SET status = 'completed' WHERE session_id IN (?, ?)",
                (sess_a, sess_b),
            )

        async def _test():
            settings = AppSettings(filepath=str(tmp_path / "settings.json"))
            settings.AI_CONSENT_GRANTED = False

            # 1. Active workspace ws_c has no completed session -> announces none available
            app_c = AutoSorterTUI(
                settings=settings, base_dir=str(ws_c), skip_wizard=True
            )
            async with app_c.run_test() as pilot:
                await pilot.press("ctrl+z")
                await pilot.pause(0.05)
                assert not isinstance(app_c.screen, RollbackConfirmModal)
                assert any(
                    "No completed sorting session available to undo" in a["message"]
                    for a in app_c.announcements
                )

            # 2. Active workspace ws_a selects sess_a (not newer sess_b) and triggers _start_scan on undo success
            app_a = AutoSorterTUI(
                settings=settings, base_dir=str(ws_a), skip_wizard=True
            )
            rescan_calls: list[str] = []
            with patch.object(
                app_a,
                "_start_scan",
                side_effect=lambda: rescan_calls.append(app_a.base_dir),
            ):
                async with app_a.run_test() as pilot:
                    await pilot.press("ctrl+z")
                    await pilot.pause(0.1)
                    assert isinstance(app_a.screen, RollbackConfirmModal)
                    assert app_a.screen.session_info["session_id"] == sess_a

                    app_a.screen.query_one("#btn-confirm-rollback", Button).press()
                    for _ in range(60):
                        if file_a.exists() and rescan_calls:
                            break
                        await pilot.pause(0.05)

                    assert file_a.exists()
                    assert dst_b.exists()  # ws_b untouched!
                    assert rescan_calls == [str(ws_a)]

        asyncio.run(_test())
    finally:
        worker.stop()
