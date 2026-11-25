"""Comprehensive tests for unified duplicate controls and two-step OS Trash ledger handoff."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import types
from pathlib import Path
from unittest import mock

import pytest
from textual.widgets import Button, DataTable, Input, Label, Select

from app.config import AppSettings, PolicyRule
from app.core.db_conn import clear_connection_cache, get_db_connection
from app.core.db_worker import DBWorker
from app.core.mover import DUPLICATE_STAGING_DIR
from app.core.moves import (
    EXPORTED_TO_TRASH,
    VERIFIED,
    SessionSummaryRow,
    query_session_summaries,
    session_summary_dicts,
)
from app.core.pipeline import (
    DropBatchCoordinator,
    PipelineLifecycleState,
    SortPipeline,
)
from app.core.plan_format import (
    count_plan_actions,
    format_plan_tree,
    format_session_rollback_outcome,
    format_trash_handoff_warning,
    format_trashed_unrestorable_warning,
    normalize_duplicate_mode,
    validate_duplicate_mode_for_headless,
)
from app.core.resilient_file_ops import DETECT_ORIGINAL_BY_MODES
from app.core.session import AppSession
from app.core.session_catalog import list_sessions
from app.core.store import open_store
from app.main import (
    _validate_headless_duplicate_mode,
    apply_config_overrides,
    build_parser,
)
from app.ui.modals.drop_zone import DropPlanConfirmModal, DropZoneModal
from app.ui.modals.edit import DuplicateReviewModal
from app.ui.modals.rules import ActiveRuleManagerModal, RuleEditModal
from app.ui.modals.session import HistoryModal, RollbackConfirmModal
from app.ui.modals.settings import SettingsModal
from app.ui.tui import AutoSorterTUI


def _find_plan_file_node(plan: dict, filename: str) -> dict | None:
    for key, val in plan.items():
        if str(key).startswith("__") or not isinstance(val, dict):
            continue
        if val.get("__type__") == "file" or val.get("node_type") == "file":
            src = str(
                val.get("source_path")
                or val.get("relative_source")
                or val.get("filepath")
                or key
            )
            if os.path.basename(src) == filename or key == filename:
                return val
        else:
            found = _find_plan_file_node(val, filename)
            if found is not None:
                return found
    return None


def test_shared_duplicate_and_trash_formatting_helpers() -> None:
    """Shared leaf helpers normalize duplicate modes and format consistent Trash/rollback messages."""
    assert normalize_duplicate_mode("  REVIEW ") == "review"
    assert normalize_duplicate_mode("unknown") == "off"
    assert normalize_duplicate_mode(None) == "off"

    assert validate_duplicate_mode_for_headless("stage", is_tui=False) is None
    assert validate_duplicate_mode_for_headless("review", is_tui=True) is None
    err_non_int = validate_duplicate_mode_for_headless(
        "review", is_tui=True, non_interactive=True
    )
    assert err_non_int is not None and "--non-interactive" in err_non_int
    err_headless = validate_duplicate_mode_for_headless("review", is_tui=False)
    assert err_headless is not None and "--tui" in err_headless

    assert format_trash_handoff_warning(None) is None
    assert format_trash_handoff_warning({"errors": {}}) is None
    warn_handoff = format_trash_handoff_warning(
        {"errors": {"/tmp/dup.pdf": "dbus unavailable"}}
    )
    assert warn_handoff is not None
    assert "dbus unavailable" in warn_handoff
    assert "reversible staging" in warn_handoff

    assert format_trashed_unrestorable_warning(0) is None
    warn_one = format_trashed_unrestorable_warning(1, session_id="sess_123")
    assert warn_one is not None and "1 duplicate copy in session 'sess_123'" in warn_one
    warn_two = format_trashed_unrestorable_warning(2)
    assert warn_two is not None and "2 duplicate copies remain in OS Trash" in warn_two

    msg_zero, ntype_zero = format_session_rollback_outcome("sess_abcd1234", 0, 2)
    assert ntype_zero == "warning"
    assert "No reversible moves" in msg_zero and "2 duplicate copies" in msg_zero

    msg_mixed, ntype_mixed = format_session_rollback_outcome("sess_abcd1234", 3, 1)
    assert ntype_mixed == "warning"
    assert "3 file(s) restored" in msg_mixed and "1 duplicate copy" in msg_mixed

    msg_clean, ntype_clean = format_session_rollback_outcome("sess_abcd1234", 4, 0)
    assert ntype_clean == "info"
    assert "successfully rolled back (4 file(s) restored)" in msg_clean


def test_migration_4_upgrades_v3_moves_check_constraint_without_data_loss(
    tmp_path: Path,
) -> None:
    """Migration 4 expands a v3 4-state moves table CHECK constraint to allow exported_to_trash without deleting rows."""
    app_dir = tmp_path / "app_v3"
    app_dir.mkdir()
    db_path = app_dir / "sortify.db"

    conn_setup = get_db_connection(str(db_path))
    with conn_setup:
        conn_setup.execute(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                timestamp REAL,
                base_dir TEXT,
                status TEXT DEFAULT 'active'
            )
            """
        )
        conn_setup.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
            ("sess_v3", 1700000000.0, str(tmp_path), "completed"),
        )
        conn_setup.execute(
            """
            CREATE TABLE moves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                src TEXT NOT NULL,
                dst TEXT NOT NULL,
                src_sha256 TEXT,
                size INTEGER,
                displaced TEXT,
                state TEXT NOT NULL CHECK (state IN ('intent', 'done', 'verified', 'failed')),
                error TEXT,
                created_at REAL,
                updated_at REAL,
                UNIQUE (session_id, seq)
            )
            """
        )
        conn_setup.execute(
            """
            INSERT INTO moves (session_id, seq, src, dst, src_sha256, size, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'verified', 1700000001.0, 1700000002.0)
            """,
            (
                "sess_v3",
                1,
                str(tmp_path / "dup.pdf"),
                str(
                    tmp_path / DUPLICATE_STAGING_DIR / "_trash" / "sess_v3" / "dup.pdf"
                ),
                "deadbeef",
                12,
            ),
        )
        conn_setup.execute("PRAGMA user_version = 3")

    clear_connection_cache()
    store = open_store(app_dir, legacy_session_root=tmp_path / "none")
    worker = DBWorker()
    try:
        db = store.database(worker)
        history = store.history(db, store.cache(worker))
        conn = get_db_connection(str(store.path))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4

        rows_before = history.moves.list_moves("sess_v3")
        assert len(rows_before) == 1
        assert rows_before[0].state == VERIFIED

        history.moves.mark_exported_to_trash("sess_v3", 1)
        rows_after = history.moves.list_moves("sess_v3")
        assert len(rows_after) == 1
        assert rows_after[0].state == EXPORTED_TO_TRASH
        assert rows_after[0].src_sha256 == "deadbeef"
    finally:
        worker.stop()


def test_query_session_summaries_legacy_ledger_fallbacks_return_seven_tuples(
    tmp_path: Path,
) -> None:
    """Legacy step_ledger and transaction_ledger fallbacks return SessionSummaryRow 7-tuples with trashed_count=0."""
    db_step = tmp_path / "legacy_step.db"
    conn_step = sqlite3.connect(str(db_step))
    with conn_step:
        conn_step.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, timestamp REAL, base_dir TEXT, status TEXT)"
        )
        conn_step.execute(
            "CREATE TABLE step_ledger (step_id INTEGER PRIMARY KEY, session_id TEXT, source_path TEXT, original_path TEXT)"
        )
        conn_step.execute(
            "INSERT INTO sessions VALUES ('s_step', 1700000100.0, '/tmp/ws', 'completed')"
        )
        conn_step.execute(
            "INSERT INTO step_ledger VALUES (1, 's_step', '/tmp/ws/a.txt', '/tmp/ws/a.txt')"
        )
    conn_step.close()
    clear_connection_cache()
    rows_step = query_session_summaries(str(db_step))
    assert rows_step == [("s_step", 1700000100.0, "/tmp/ws", "completed", 1, 1, 0)]
    assert isinstance(rows_step[0], SessionSummaryRow)
    assert rows_step[0].trashed_count == 0
    dicts_step = session_summary_dicts(str(db_step), history_db_path=str(db_step))
    assert dicts_step[0]["history_db_path"] == str(db_step)
    assert dicts_step[0]["trashed_count"] == 0

    db_tx = tmp_path / "legacy_tx.db"
    conn_tx = sqlite3.connect(str(db_tx))
    with conn_tx:
        conn_tx.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, timestamp REAL, base_dir TEXT, status TEXT)"
        )
        conn_tx.execute(
            "CREATE TABLE transaction_ledger (id INTEGER PRIMARY KEY, session_id TEXT, source_path TEXT)"
        )
        conn_tx.execute(
            "INSERT INTO sessions VALUES ('s_tx', 1700000200.0, '/tmp/ws2', 'completed')"
        )
        conn_tx.execute(
            "INSERT INTO transaction_ledger VALUES (1, 's_tx', '/tmp/ws2/b.txt')"
        )
    conn_tx.close()
    clear_connection_cache()
    rows_tx = query_session_summaries(str(db_tx))
    assert rows_tx == [("s_tx", 1700000200.0, "/tmp/ws2", "completed", 1, 1, 0)]


def test_flush_trash_marks_exported_to_trash_preserves_rows_and_handles_undo_warnings(
    tmp_path: Path,
) -> None:
    """Flushing duplicates transitions rows to exported_to_trash (no DELETE), keeps failed handoffs in verified, and separates session counts."""
    store_dir = tmp_path / "store"
    ws = tmp_path / "workspace"
    ws.mkdir()

    payload = b"Exact byte duplicate payload 2026"
    orig = ws / "contract.pdf"
    dup_ok = ws / "contract (1).pdf"
    dup_fail = ws / "contract (2).pdf"
    orig.write_bytes(payload)
    dup_ok.write_bytes(payload)
    dup_fail.write_bytes(payload)

    with mock.patch.dict(os.environ, {"AUTOSORTER_APP_DIR": str(store_dir)}):
        settings = AppSettings()
        settings.override("AI_CONSENT_GRANTED", False)
        settings.override("DUPLICATE_ACTION", "trash")
        settings.override("DETECT_ORIGINAL_BY", "name")
        settings.override("KEYWORD_RULES", {"contract": "Contracts"})

        session = AppSession(settings, base_dir=str(ws))
        try:
            pipeline = SortPipeline(session)
            _plan, result = pipeline.run(flush_trash=False)
            assert result is not None and result.ok
            sid = result.session_id
            assert sid is not None

            # Without --flush-trash, trash-mode duplicates remain parked in reversible staging
            assert len(result.moved) == 1
            assert len(result.staged) == 2
            assert len(result.trashed) == 0

            # Flush with partial failure: dup_ok succeeds, dup_fail raises OSError
            exported_targets: list[str] = []

            def partial_trash(target: str) -> None:
                if "contract (2)" in os.path.basename(target):
                    raise OSError("Trash daemon unavailable for second copy")
                exported_targets.append(target)
                os.remove(target)

            report = pipeline.flush_trash(
                sid, trash_fn=partial_trash, trashed_only=True
            )
            assert len(report["exported"]) == 1
            assert len(report["kept_staged"]) == 1
            assert len(report["errors"]) == 1

            # Verify append-only MoveLedger kept ALL 3 rows (no DELETE FROM moves!)
            moves = session.history_manager.moves.list_moves(sid)
            assert len(moves) == 3
            by_src = {os.path.basename(m.src): m for m in moves}
            assert by_src["contract.pdf"].state == VERIFIED
            assert by_src["contract (1).pdf"].state == EXPORTED_TO_TRASH
            assert by_src["contract (2).pdf"].state == VERIFIED

            # Verify session summaries count reversible steps separately from exported_to_trash
            cat_sessions = {
                s["session_id"]: s for s in list_sessions(store=session.store)
            }
            hist_sessions = {
                s["session_id"]: s for s in session.history_manager.get_sessions()
            }
            for mapping in (cat_sessions, hist_sessions):
                assert mapping[sid]["step_count"] == 2  # orig + kept_staged dup_fail
                assert mapping[sid]["trashed_count"] == 1

            # Undo restores BOTH the moved primary AND the kept_staged duplicate, and records the 1 trashed copy
            undo_result = pipeline.undo(sid)
            assert undo_result.ok
            assert orig.read_bytes() == payload
            assert dup_fail.read_bytes() == payload
            assert not dup_ok.exists()
            assert len(undo_result.moved) == 2
            assert len(undo_result.trashed) == 1
            assert undo_result.summary["trashed_unrestorable"] == 1
            assert undo_result.summary["trashed_paths"] == ["contract (1).pdf"]
        finally:
            session.close()


def test_trash_only_session_omits_undo_hint_and_rolls_back_cleanly(
    tmp_path: Path,
) -> None:
    """When primary stays in place and only a duplicate is exported to OS Trash, reversible count is 0 and undo handles trash-only session cleanly."""
    store_dir = tmp_path / "store_trash_only"
    ws = tmp_path / "ws_trash_only"
    ws.mkdir()

    payload = b"Root-level note with identical backup copy"
    orig = ws / "readme.txt"
    dup = ws / "readme (1).txt"
    orig.write_bytes(payload)
    dup.write_bytes(payload)

    with mock.patch.dict(os.environ, {"AUTOSORTER_APP_DIR": str(store_dir)}):
        settings = AppSettings()
        settings.override("AI_CONSENT_GRANTED", False)
        settings.override("DUPLICATE_ACTION", "trash")
        settings.override("DETECT_ORIGINAL_BY", "name")

        plan = {
            "readme.txt": {
                "__type__": "file",
                "node_type": "file",
                "filepath": str(orig),
                "source_path": "readme.txt",
                "relative_source": "readme.txt",
                "target_filename": "readme.txt",
                "is_checked": True,
            },
            "readme (1).txt": {
                "__type__": "file",
                "node_type": "file",
                "filepath": str(dup),
                "source_path": "readme (1).txt",
                "relative_source": "readme (1).txt",
                "target_filename": "readme (1).txt",
                "is_checked": True,
                "is_duplicate": True,
                "duplicate_of": str(orig),
                "duplicate_action": "trash",
                "duplicate_resolved": True,
            },
        }
        counts = count_plan_actions(plan)
        assert counts["trashed"] == 1
        tree_lines, n_files, _n_folders = format_plan_tree(plan)
        assert n_files == 1
        assert any("[DUPLICATE: trash]" in line for line in tree_lines)

        session = AppSession(settings, base_dir=str(ws))
        try:
            pipeline = SortPipeline(session)
            pipeline.transition_to(PipelineLifecycleState.PLANNED)
            result = pipeline.execute(
                plan,
                flush_trash=True,
                flush_trashed_only=True,
                trash_fn=lambda p: os.remove(p),
            )
            assert result.ok
            assert len(result.moved) == 0
            assert len(result.staged) == 0
            assert len(result.trashed) == 1
            assert "1 trashed" in result.describe()

            sid = result.session_id
            assert sid is not None
            sess_summary = next(
                s
                for s in session.history_manager.get_sessions()
                if s["session_id"] == sid
            )
            assert sess_summary["step_count"] == 0
            assert sess_summary["trashed_count"] == 1

            undo_res = pipeline.undo(sid)
            assert undo_res.ok
            assert len(undo_res.moved) == 0
            assert len(undo_res.trashed) == 1
            assert undo_res.summary["trashed_unrestorable"] == 1
            sess_after = next(
                s
                for s in session.history_manager.get_sessions()
                if s["session_id"] == sid
            )
            assert sess_after["status"] == "rolled_back"
        finally:
            session.close()


def test_analyzer_tags_automatic_modes_resolved_and_review_unresolved(
    tmp_path: Path,
) -> None:
    """SortPipeline.plan() marks skip, stage, and trash duplicate items duplicate_resolved=True and leaves review unresolved."""
    store_dir = tmp_path / "an_store"
    ws = tmp_path / "an_ws"
    ws.mkdir()
    f1 = ws / "a.txt"
    f2 = ws / "b.txt"
    f1.write_bytes(b"same-bytes")
    f2.write_bytes(b"same-bytes")

    with mock.patch.dict(os.environ, {"AUTOSORTER_APP_DIR": str(store_dir)}):
        for mode, expected_resolved in (
            ("skip", True),
            ("stage", True),
            ("trash", True),
            ("review", False),
        ):
            settings = AppSettings()
            settings.override("AI_CONSENT_GRANTED", False)
            settings.override("DUPLICATE_ACTION", mode)
            settings.override("DETECT_ORIGINAL_BY", "name")
            session = AppSession(settings, base_dir=str(ws))
            try:
                plan = SortPipeline(session).plan()
                dup_node = _find_plan_file_node(plan, "b.txt")
                assert dup_node is not None
                assert dup_node.get("is_duplicate") is True
                assert (
                    bool(dup_node.get("duplicate_resolved", False)) is expected_resolved
                )
            finally:
                session.close()


@pytest.mark.anyio
@pytest.mark.parametrize("auto_mode", ["off", "skip", "stage", "trash"])
async def test_tui_execute_sort_auto_applies_non_review_duplicate_modes(
    tmp_path: Path, auto_mode: str
) -> None:
    """AutoSorterTUI.action_execute_sort applies off/skip/stage/trash automatically without opening DuplicateReviewModal."""
    f1 = tmp_path / "alpha.pdf"
    f2 = tmp_path / "beta.pdf"
    f1.write_bytes(b"duplicate-pdf-bytes")
    f2.write_bytes(b"duplicate-pdf-bytes")

    plan = {
        "Docs": {
            "alpha.pdf": {
                "node_type": "file",
                "relative_source": "alpha.pdf",
                "target_filename": "alpha.pdf",
                "is_checked": True,
            },
            "beta.pdf": {
                "node_type": "file",
                "relative_source": "beta.pdf",
                "target_filename": "beta.pdf",
                "is_checked": True,
            },
        }
    }

    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    settings.override("DUPLICATE_ACTION", auto_mode)
    settings.override("DETECT_ORIGINAL_BY", "name")
    app = AutoSorterTUI(settings=settings)
    mock_session = mock.MagicMock()
    mock_session.base_dir = str(tmp_path)
    app.app_session = mock_session
    app.plan = plan

    async with app.run_test() as pilot:
        with mock.patch.object(app, "run_execute_worker") as mock_worker:
            app.action_execute_sort()
            await pilot.pause()

            assert not isinstance(app.screen, DuplicateReviewModal)
            mock_worker.assert_called_once()
            dup_node = plan["Docs"]["beta.pdf"]
            if auto_mode == "off":
                assert dup_node.get("duplicate_action") is None
                assert not dup_node.get("is_excluded")
            elif auto_mode == "skip":
                assert dup_node.get("is_excluded") is True
                assert dup_node.get("is_checked") is False
            elif auto_mode == "stage":
                assert dup_node.get("duplicate_action") == "stage"
                assert app._pending_flush_trash is False
            elif auto_mode == "trash":
                assert dup_node.get("duplicate_action") == "trash"
                assert app._pending_flush_trash is True


@pytest.mark.anyio
async def test_settings_modal_exposes_and_persists_duplicate_controls(
    tmp_path: Path,
) -> None:
    """SettingsModal loads and saves DUPLICATE_ACTION and DETECT_ORIGINAL_BY and AutoSorterTUI persists them."""
    cfg_path = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(cfg_path))
    settings.AI_CONSENT_GRANTED = False
    settings.DUPLICATE_ACTION = "stage"
    settings.DETECT_ORIGINAL_BY = "created"
    settings.flush()

    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        app.action_open_settings()
        await pilot.pause()
        assert isinstance(app.screen, SettingsModal)
        modal = app.screen

        sel_dup = modal.query_one("#select-duplicate-action", Select)
        sel_orig = modal.query_one("#select-detect-original-by", Select)
        assert sel_dup.value == "stage"
        assert sel_orig.value == "created"
        orig_values = {
            val for _label, val in sel_orig._options if val is not Select.BLANK
        }
        assert orig_values == set(DETECT_ORIGINAL_BY_MODES)

        sel_dup.value = "review"
        sel_orig.value = "-lastmodified"
        await pilot.pause()

        with mock.patch.object(app, "_start_scan"):
            modal.action_save()
            await pilot.pause()

        assert settings.DUPLICATE_ACTION == "review"
        assert settings.DETECT_ORIGINAL_BY == "-lastmodified"

    reloaded = AppSettings(filepath=str(cfg_path))
    assert reloaded.DUPLICATE_ACTION == "review"
    assert reloaded.DETECT_ORIGINAL_BY == "-lastmodified"


@pytest.mark.anyio
async def test_duplicate_review_modal_escapes_badges_and_warns_on_trash() -> None:
    """DuplicateReviewModal renders [Original], [Duplicate], [Original on Disk], and bracketed filenames intact, and warns when trash is selected."""
    groups = [
        {
            "group_id": "g_disk",
            "target_filename": "invoice[2026].pdf",
            "target_dir": "/tmp/Invoices[Q1]",
            "existing_file_on_disk": True,
            "primary_source_path": "/tmp/Invoices[Q1]/invoice[2026].pdf",
            "is_byte_duplicate": True,
            "default_action": "stage",
            "items": [
                {
                    "node_key": "invoice[2026].pdf",
                    "source_path": "/tmp/incoming/invoice[2026].pdf",
                    "relative_source": "incoming/invoice[2026].pdf",
                },
            ],
        },
        {
            "group_id": "g_cluster",
            "target_filename": "report[Draft].pdf",
            "target_dir": "/tmp/Reports",
            "existing_file_on_disk": False,
            "is_byte_duplicate": True,
            "default_action": "stage",
            "items": [
                {
                    "node_key": "report[Draft].pdf",
                    "source_path": "/tmp/report[Draft].pdf",
                    "relative_source": "report[Draft].pdf",
                    "is_primary": True,
                },
                {
                    "node_key": "report[Draft] (1).pdf",
                    "source_path": "/tmp/report[Draft] (1).pdf",
                    "relative_source": "report[Draft] (1).pdf",
                },
            ],
        },
    ]
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = False
    app = AutoSorterTUI(settings=settings)
    modal = DuplicateReviewModal(duplicate_groups=groups)

    async with app.run_test(size=(120, 36)) as pilot:
        await app.push_screen(modal)
        await pilot.pause()

        rendered_labels = [str(lbl.render()) for lbl in modal.query(Label)]
        joined = "\n".join(rendered_labels)
        assert "[Original on Disk]" in joined
        assert "[Original]" in joined
        assert "[Duplicate]" in joined
        assert "invoice[2026].pdf" in joined
        assert "Invoices[Q1]" in joined
        assert "report[Draft].pdf" in joined

        warn_lbl = modal.query_one("#lbl-trash-warning", Label)
        assert warn_lbl.has_class("hidden")

        sel = modal.query_one("#select-action-g_disk", Select)
        sel.value = "trash"
        await pilot.pause()

        assert not warn_lbl.has_class("hidden")
        assert "cannot be undone" in str(warn_lbl.render())


def test_cli_duplicates_review_validation_and_drop_flush_trash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI validates --duplicates review against headless/TUI flags and supports drop --flush-trash with separated counts and error surfacing."""
    parser = build_parser()

    # 1. Headless --duplicates review rejected with exit_code=2 (unit + real CLI subprocess)
    args_headless_sort = parser.parse_args(
        ["sort", str(tmp_path), "--duplicates", "review"]
    )
    settings = AppSettings()
    apply_config_overrides(settings, args_headless_sort)
    with pytest.raises(SystemExit) as exc_info:
        _validate_headless_duplicate_mode(args_headless_sort, settings)
    assert exc_info.value.code == 2
    err_out = capsys.readouterr().err
    assert "--tui" in err_out and "--interactive" in err_out

    env = dict(
        os.environ,
        AUTOSORTER_APP_DIR=str(tmp_path / "cli_env"),
        PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring",
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.main",
            "sort",
            str(tmp_path),
            "--duplicates",
            "review",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 2
    assert "review" in proc.stderr

    # 2. Accepted by parser with --tui or --interactive
    args_tui = parser.parse_args(["--tui", "--duplicates", "review"])
    settings_tui = AppSettings()
    apply_config_overrides(settings_tui, args_tui)
    assert settings_tui.DUPLICATE_ACTION == "review"

    args_interactive = parser.parse_args(
        ["sort", str(tmp_path), "--duplicates", "review", "--interactive"]
    )
    settings_int = AppSettings()
    apply_config_overrides(settings_int, args_interactive)
    assert settings_int.DUPLICATE_ACTION == "review"
    assert args_interactive.interactive is True

    # 3. DropBatchCoordinator --duplicates trash with and without flush_trash + error surfacing
    store_dir = tmp_path / "drop_store"
    target_dir = tmp_path / "target"
    incoming = tmp_path / "incoming"
    target_dir.mkdir()
    incoming.mkdir()
    f_orig = incoming / "receipt.txt"
    f_dup = incoming / "receipt_copy.txt"
    f_orig.write_bytes(b"Receipt #8899 total $42.00")
    f_dup.write_bytes(b"Receipt #8899 total $42.00")

    with mock.patch.dict(os.environ, {"AUTOSORTER_APP_DIR": str(store_dir)}):
        drop_settings = AppSettings()
        drop_settings.override("AI_CONSENT_GRANTED", False)
        drop_settings.override("DUPLICATE_ACTION", "trash")
        drop_settings.override("DETECT_ORIGINAL_BY", "name")

        coord = DropBatchCoordinator(drop_settings)
        bp1 = coord.plan_batch([str(f_orig), str(f_dup)], dest_dir=str(target_dir))
        res_staged = coord.execute_batch(bp1, flush_trash=False)
        assert res_staged.moved_count == 1
        assert res_staged.staged_count == 1
        assert res_staged.trashed_count == 0

        # Second batch with flush_trash=True and simulated OS Trash error surfaces errors and keeps copy staged
        target_dir2 = tmp_path / "target2"
        target_dir2.mkdir()
        f_orig2 = incoming / "memo.txt"
        f_dup2 = incoming / "memo_copy.txt"
        f_orig2.write_bytes(b"Memo body bytes")
        f_dup2.write_bytes(b"Memo body bytes")

        args_drop = parser.parse_args(
            [
                "drop",
                str(target_dir2),
                str(f_orig2),
                str(f_dup2),
                "--duplicates",
                "trash",
                "--flush-trash",
            ]
        )
        assert args_drop.flush_trash is True

        def fail_trash(_p: str) -> None:
            raise OSError("Simulated OS Trash permission denied")

        bp2 = coord.plan_batch([str(f_orig2), str(f_dup2)], dest_dir=str(target_dir2))
        res_err = coord.execute_batch(
            bp2,
            flush_trash=True,
            flush_trashed_only=True,
            trash_fn=fail_trash,
        )
        assert res_err.moved_count == 1
        assert res_err.staged_count == 1
        assert res_err.trashed_count == 0
        assert res_err.trash_handoff is not None
        assert len(res_err.trash_handoff["errors"]) == 1


@pytest.mark.anyio
async def test_drop_zone_modal_routes_review_duplicates_through_duplicate_review_modal(
    tmp_path: Path,
) -> None:
    """DropZoneModal opens DuplicateReviewModal when DUPLICATE_ACTION == 'review' before DropPlanConfirmModal."""
    target_dir = tmp_path / "drop_target"
    incoming = tmp_path / "drop_in"
    target_dir.mkdir()
    incoming.mkdir()
    p1 = incoming / "spec.pdf"
    p2 = incoming / "spec (1).pdf"
    p1.write_bytes(b"identical-drop-bytes")
    p2.write_bytes(b"identical-drop-bytes")

    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = False
    settings.override("DUPLICATE_ACTION", "review")
    settings.override("DETECT_ORIGINAL_BY", "name")

    app = AutoSorterTUI(settings=settings, base_dir=str(target_dir))
    drop_modal = DropZoneModal(base_dir=str(target_dir), settings=settings)

    plan = {
        "Docs": {
            "spec.pdf": {
                "__type__": "file",
                "node_type": "file",
                "filepath": str(p1),
                "source_path": str(p1),
                "relative_source": str(p1),
                "target_filename": "spec.pdf",
                "is_checked": True,
            },
            "spec (1).pdf": {
                "__type__": "file",
                "node_type": "file",
                "filepath": str(p2),
                "source_path": str(p2),
                "relative_source": str(p2),
                "target_filename": "spec (1).pdf",
                "is_checked": True,
            },
        }
    }

    async with app.run_test(size=(120, 36)) as pilot:
        await app.push_screen(drop_modal)
        await pilot.pause()

        drop_modal._pending_drop = [
            {
                "session": types.SimpleNamespace(
                    base_dir=str(target_dir), close=lambda: None
                ),
                "plan": plan,
                "paths": [str(p1), str(p2)],
            }
        ]
        drop_modal._pending_batch_plan = None
        drop_modal._drop_duplicates_reviewed = False
        drop_modal._confirm_pending_drop()
        await pilot.pause()

        assert isinstance(app.screen, DuplicateReviewModal)
        rev_modal = app.screen
        gid = rev_modal.duplicate_groups[0]["group_id"]
        rev_modal.query_one(f"#select-action-{gid}", Select).value = "trash"
        await pilot.pause()

        rev_modal.action_confirm()
        await pilot.pause()

        assert isinstance(app.screen, DropPlanConfirmModal)
        assert drop_modal._pending_drop_flush_trash is True
        assert plan["Docs"]["spec (1).pdf"]["duplicate_action"] == "trash"

        # Non-review mode ('stage') skips DuplicateReviewModal and opens DropPlanConfirmModal directly
        app.pop_screen()
        await pilot.pause()
        settings.override("DUPLICATE_ACTION", "stage")
        plan["Docs"]["spec (1).pdf"].pop("duplicate_resolved", None)
        plan["Docs"]["spec (1).pdf"]["duplicate_action"] = "stage"
        drop_modal._pending_drop = [
            {
                "session": types.SimpleNamespace(
                    base_dir=str(target_dir), close=lambda: None
                ),
                "plan": plan,
                "paths": [str(p1), str(p2)],
            }
        ]
        drop_modal._drop_duplicates_reviewed = False
        drop_modal._confirm_pending_drop()
        await pilot.pause()
        assert isinstance(app.screen, DropPlanConfirmModal)
        assert drop_modal._pending_drop_flush_trash is False


@pytest.mark.anyio
async def test_active_rule_manager_shows_action_column_and_saves_empty_target_disposal_rules(
    tmp_path: Path,
) -> None:
    """ActiveRuleManagerModal displays an Action column and RuleEditModal saves simple and compound trash/duplicate rules with empty target_path."""
    rules: list[dict[str, object]] = [
        dict(
            PolicyRule(
                type="pattern",
                expression="*.tmp",
                target_path="",
                priority=1,
                action="trash",
            )
        ),
        dict(
            PolicyRule(
                type="duplicate",
                expression="*.pdf",
                target_path="",
                priority=2,
                action="quarantine",
                detect_original_by="created",
            )
        ),
        dict(
            PolicyRule(
                type="pattern",
                expression="temp_compound",
                target_path="",
                priority=3,
                action="trash",
                match_mode="all",
                conditions=[
                    {"field": "extension", "operator": "equals", "value": ".bak"}
                ],
            )
        ),
    ]
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings._settings_model.AI_CONSENT_GRANTED = False
    settings.POLICIES = rules
    app = AutoSorterTUI(settings=settings)
    manager = ActiveRuleManagerModal(settings)

    async with app.run_test(size=(120, 36)) as pilot:
        await app.push_screen(manager)
        await pilot.pause()

        table = manager.query_one("#rules-table", DataTable)
        col_labels = [str(col.label) for col in table.columns.values()]
        assert col_labels == [
            "Status",
            "Type",
            "Action",
            "Pattern",
            "Target Folder",
            "Priority",
        ]
        row0 = [str(cell) for cell in table.get_row_at(0)]
        row1 = [str(cell) for cell in table.get_row_at(1)]
        row2 = [str(cell) for cell in table.get_row_at(2)]
        assert row0[2] == "trash"
        assert row1[2] == "quarantine"
        assert row2[1] == "pattern [all]"
        assert row2[2] == "trash"

        # Open RuleEditModal for simple trash, duplicate, and compound trash policy rules with empty target_path
        saved_results: list[dict[str, object] | None] = []
        edit_trash = RuleEditModal(
            title="Edit Trash Policy",
            pattern="*.tmp",
            target_folder="",
            rule_kind="policy",
            policy_type="pattern",
            priority=1,
            action="trash",
            settings=settings,
        )
        await app.push_screen(edit_trash, saved_results.append)
        await pilot.pause()

        assert edit_trash.query_one("#input-rule-target", Input).value == ""
        edit_trash.action_save()
        await pilot.pause()

        edit_dup = RuleEditModal(
            title="Edit Duplicate Policy",
            pattern="*.pdf",
            target_folder="",
            rule_kind="policy",
            policy_type="duplicate",
            priority=2,
            action="trash",
            settings=settings,
        )
        await app.push_screen(edit_dup, saved_results.append)
        await pilot.pause()
        edit_dup.action_save()
        await pilot.pause()

        edit_compound = RuleEditModal(
            title="Edit Compound Trash Policy",
            pattern="temp_compound",
            target_folder="",
            rule_kind="policy",
            policy_type="pattern",
            priority=3,
            action="trash",
            policy=dict(rules[2]),
            settings=settings,
        )
        await app.push_screen(edit_compound, saved_results.append)
        await pilot.pause()
        edit_compound.action_save()
        await pilot.pause()

        assert len(saved_results) == 3
        assert saved_results[0] is not None
        pol0 = saved_results[0]["policy"]
        assert isinstance(pol0, dict)
        assert pol0["type"] == "pattern"
        assert pol0["action"] == "trash"
        assert pol0["target_path"] == ""

        assert saved_results[1] is not None
        pol1 = saved_results[1]["policy"]
        assert isinstance(pol1, dict)
        assert pol1["type"] == "duplicate"
        assert pol1["action"] == "trash"
        assert pol1["target_path"] == ""

        assert saved_results[2] is not None
        pol2 = saved_results[2]["policy"]
        assert isinstance(pol2, dict)
        assert pol2["action"] == "trash"
        assert pol2["target_path"] == ""
        assert "conditions" in pol2

        # Round-trip edit through ActiveRuleManagerModal preserves imported detect_original_by
        table.move_cursor(row=1)
        await pilot.pause()
        manager.action_edit_rule()
        await pilot.pause()
        assert isinstance(app.screen, RuleEditModal)
        app.screen.query_one("#input-policy-priority", Input).value = "9"
        await pilot.pause()
        app.screen.action_save()
        await pilot.pause()
        assert settings.POLICIES[1]["priority"] == 9
        assert settings.POLICIES[1]["target_path"] == ""
        assert settings.POLICIES[1]["detect_original_by"] == "created"


@pytest.mark.anyio
async def test_history_and_rollback_modals_handle_trash_only_and_mixed_sessions(
    tmp_path: Path,
) -> None:
    """RollbackConfirmModal and HistoryModal surface OS Trash warnings and handle trash-only sessions cleanly."""
    trash_only_sess = {
        "session_id": "sess_trash_only_1234",
        "timestamp": 1700000500.0,
        "base_dir": str(tmp_path),
        "status": "completed",
        "step_count": 0,
        "file_count": 0,
        "trashed_count": 2,
    }
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = False
    app = AutoSorterTUI(settings=settings)

    async with app.run_test(size=(120, 36)) as pilot:
        confirm = RollbackConfirmModal(session_info=trash_only_sess)
        await app.push_screen(confirm)
        await pilot.pause()

        trash_warn = confirm.query_one("#lbl-trashed-warning", Label)
        assert "OS Trash" in str(trash_warn.render())
        assert not confirm.query("#chk-ignore-missing")
        app.pop_screen()
        await pilot.pause()

        hist_modal = HistoryModal(sessions=[trash_only_sess], base_dir=str(tmp_path))
        await app.push_screen(hist_modal)
        await pilot.pause()

        select_sess = hist_modal.query_one("#select-history-session", Select)
        select_sess.value = "sess_trash_only_1234"
        await pilot.pause()
        btn_rb = hist_modal.query_one("#btn-rollback-session", Button)
        assert btn_rb.disabled is False


def test_cli_sort_and_undo_separate_counts_surface_trash_errors_and_omit_zero_reversible_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI sort separates moved/staged/trashed counts, surfaces OS Trash errors, omits undo hint when 0 moves are reversible, and undo warns on trashed copies."""
    import app.main as main_mod

    store_dir = tmp_path / "cli_store"
    ws = tmp_path / "cli_ws"
    ws.mkdir()
    orig = ws / "invoice_001.txt"
    dup = ws / "invoice_001_copy.txt"
    orig.write_bytes(b"Invoice #001 exact bytes")
    dup.write_bytes(b"Invoice #001 exact bytes")

    with mock.patch.dict(os.environ, {"AUTOSORTER_APP_DIR": str(store_dir)}):

        def _run_main(argv: list[str]) -> tuple[str, str]:
            with (
                mock.patch.object(sys, "argv", argv),
                pytest.raises(SystemExit) as exc,
            ):
                main_mod.main()
            assert exc.value.code == 0
            cap = capsys.readouterr()
            return cap.out, cap.err

        # 1. Simulate OS Trash handoff failure on CLI sort --duplicates trash --flush-trash
        with mock.patch(
            "send2trash.send2trash",
            side_effect=OSError("Headless dbus trash unavailable"),
        ):
            out1, err1 = _run_main(
                [
                    "sortify",
                    "--decline-ai-consent",
                    "sort",
                    str(ws),
                    "--duplicates",
                    "trash",
                    "--detect-original-by",
                    "name",
                    "--flush-trash",
                ]
            )
        assert "[DUPLICATE: trash]" in out1
        assert "1 moved, 1 staged" in err1
        assert "OS Trash handoff failed" in err1
        assert "undo --latest" in err1

        # Undo restores both moved primary and kept-staged duplicate
        out_u1, err_u1 = _run_main(
            ["sortify", "--decline-ai-consent", "undo", "--latest"]
        )
        assert "Rollback completed successfully" in (out_u1 + err_u1)
        assert orig.exists() and dup.exists()

        # 2. Succeeding OS Trash handoff on mixed session -> undo warns trashed copy remains in OS Trash
        with mock.patch(
            "send2trash.send2trash",
            side_effect=lambda p: os.remove(p),
        ):
            _out2, err2 = _run_main(
                [
                    "sortify",
                    "--decline-ai-consent",
                    "sort",
                    str(ws),
                    "--duplicates",
                    "trash",
                    "--detect-original-by",
                    "name",
                    "--flush-trash",
                ]
            )
        assert "1 moved, 1 trashed" in err2
        assert "undo --latest" in err2

        _out_u2, err_u2 = _run_main(
            ["sortify", "--decline-ai-consent", "undo", "--latest"]
        )
        assert "OS Trash" in err_u2
        assert orig.exists() and not dup.exists()


@pytest.mark.anyio
async def test_tui_ctrl_z_undo_handles_trash_only_and_mixed_sessions(
    tmp_path: Path,
) -> None:
    """AutoSorterTUI.action_undo_last_sort handles trash-only sessions and warns when trashed copies remain in OS Trash."""
    store_dir = tmp_path / "tui_undo_store"
    ws = tmp_path / "tui_undo_ws"
    ws.mkdir()
    orig = ws / "doc.txt"
    dup = ws / "doc (1).txt"
    orig.write_bytes(b"TUI undo duplicate bytes")
    dup.write_bytes(b"TUI undo duplicate bytes")

    with mock.patch.dict(os.environ, {"AUTOSORTER_APP_DIR": str(store_dir)}):
        settings = AppSettings()
        settings.override("AI_CONSENT_GRANTED", False)
        settings.override("DUPLICATE_ACTION", "trash")
        settings.override("DETECT_ORIGINAL_BY", "name")

        # Create a trash-only completed session (orig stays in place, dup exported to OS Trash)
        plan = {
            "doc.txt": {
                "__type__": "file",
                "node_type": "file",
                "filepath": str(orig),
                "source_path": "doc.txt",
                "relative_source": "doc.txt",
                "target_filename": "doc.txt",
                "is_checked": True,
            },
            "doc (1).txt": {
                "__type__": "file",
                "node_type": "file",
                "filepath": str(dup),
                "source_path": "doc (1).txt",
                "relative_source": "doc (1).txt",
                "target_filename": "doc (1).txt",
                "is_checked": True,
                "is_duplicate": True,
                "duplicate_of": str(orig),
                "duplicate_action": "trash",
                "duplicate_resolved": True,
            },
        }
        session = AppSession(settings, base_dir=str(ws))
        try:
            pipeline = SortPipeline(session)
            pipeline.transition_to(PipelineLifecycleState.PLANNED)
            res = pipeline.execute(
                plan,
                flush_trash=True,
                flush_trashed_only=True,
                trash_fn=lambda p: os.remove(p),
            )
            assert res.ok and len(res.trashed) == 1
        finally:
            session.close()

        app = AutoSorterTUI(settings=settings, base_dir=str(ws))
        announced: list[str] = []
        orig_announce = app.announce

        def track_announce(
            message: str,
            priority: str = "polite",
            help_url: str | None = None,
        ) -> str:
            announced.append(str(message))
            return orig_announce(message, priority=priority, help_url=help_url)

        setattr(app, "announce", track_announce)
        async with app.run_test(size=(120, 36)) as pilot:
            with mock.patch.object(app, "_start_scan"):
                app.action_undo_last_sort()
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()

        assert any("OS Trash" in m for m in announced)


def test_session_summaries_preserve_failed_step_counts_while_separating_exported_to_trash(
    tmp_path: Path,
) -> None:
    """query_session_summaries and list_sessions count failed moves in step_count/file_count while separating exported_to_trash and keeping failed sessions non-undoable."""
    from app.core.history import open_session_history
    from app.core.moves import FAILED, VERIFIED
    from app.core.session_catalog import has_remaining_undo_moves, list_sessions
    from app.core.store import open_store

    store_dir = tmp_path / "summary_store"
    ws = tmp_path / "summary_ws"
    ws.mkdir()
    with mock.patch.dict(os.environ, {"AUTOSORTER_APP_DIR": str(store_dir)}):
        store = open_store()
        with open_session_history(store) as history:
            ledger = history.moves

            # 1. Failed session with one failed move
            sid_failed = history.start_session(str(ws))
            seq_f = ledger.record_intent(
                sid_failed,
                str(ws / "broken.txt"),
                str(ws / "Docs" / "broken.txt"),
                None,
                12,
            )
            ledger.mark_failed(sid_failed, seq_f, "disk full")
            history._transition_session_status(sid_failed, "failed")

            # 2. Mixed completed session with one verified move + one exported_to_trash duplicate
            sid_mixed = history.start_session(str(ws))
            seq_v = ledger.record_intent(
                sid_mixed,
                str(ws / "orig.txt"),
                str(ws / "Docs" / "orig.txt"),
                "abc",
                10,
            )
            ledger.mark_done(sid_mixed, seq_v, str(ws / "Docs" / "orig.txt"))
            ledger.mark_verified(sid_mixed, seq_v)
            seq_t = ledger.record_intent(
                sid_mixed,
                str(ws / "dup.txt"),
                str(ws / ".sortify_duplicates" / sid_mixed / "_trash" / "dup.txt"),
                "abc",
                10,
            )
            ledger.mark_done(
                sid_mixed,
                seq_t,
                str(ws / ".sortify_duplicates" / sid_mixed / "_trash" / "dup.txt"),
            )
            ledger.mark_verified(sid_mixed, seq_t)
            ledger.mark_exported_to_trash(sid_mixed, seq_t)
            history.complete_session(sid_mixed)

            by_hist = {s["session_id"]: s for s in history.get_sessions()}

        by_cat = {s["session_id"]: s for s in list_sessions(store)}
        for mapping in (by_hist, by_cat):
            assert mapping[sid_failed]["status"] == FAILED
            assert mapping[sid_failed]["step_count"] == 1
            assert mapping[sid_failed]["file_count"] == 1
            assert mapping[sid_failed]["trashed_count"] == 0

            assert mapping[sid_mixed]["status"] == "completed"
            assert mapping[sid_mixed]["step_count"] == 1
            assert mapping[sid_mixed]["file_count"] == 1
            assert mapping[sid_mixed]["trashed_count"] == 1
            assert VERIFIED != FAILED

        assert has_remaining_undo_moves(by_cat[sid_failed], store=store) is False
        assert has_remaining_undo_moves(by_cat[sid_mixed], store=store) is True


def test_unencrypted_legacy_store_upgrade_to_v4_and_worker_rollback_preserves_wal_shm(
    tmp_path: Path,
) -> None:
    """Upgrading an unencrypted stdlib-sqlite3 v2 store to v4 on MainThread and probing via HistoryManager DBWorker must not corrupt WAL -shm locks across threads."""
    import sqlite3 as stdlib_sqlite3

    from app.core.db_conn import get_db_connection
    from app.core.history import open_session_history
    from app.core.store import _store_initial, open_store

    app_dir = tmp_path / "legacy_unencrypted_app"
    app_dir.mkdir()
    ws = tmp_path / "legacy_ws"
    (ws / "Sorted").mkdir(parents=True)
    trash_stage = ws / ".sortify_duplicates" / "s-legacy" / "_trash"
    trash_stage.mkdir(parents=True)

    moved_dst = ws / "Sorted" / "report.txt"
    moved_dst.write_text("report body", encoding="utf-8")
    staged_dup = trash_stage / "report_copy.txt"
    staged_dup.write_text("report body", encoding="utf-8")

    # Build an unencrypted legacy v2 SQLite store with no key file using stdlib sqlite3
    db_file = app_dir / "sortify.db"
    raw = stdlib_sqlite3.connect(str(db_file))
    _store_initial(raw)
    raw.execute("DROP TABLE IF EXISTS moves")
    raw.execute(
        """
        CREATE TABLE moves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            src TEXT NOT NULL,
            dst TEXT NOT NULL,
            src_sha256 TEXT,
            size INTEGER,
            displaced TEXT,
            state TEXT NOT NULL CHECK (state IN ('intent', 'done', 'verified', 'failed')),
            error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE (session_id, seq)
        )
        """
    )
    raw.execute(
        "INSERT INTO sessions VALUES ('s-legacy', 1700000100.0, ?, 'completed')",
        (str(ws),),
    )
    raw.execute(
        "INSERT INTO moves (session_id, seq, src, dst, state, created_at, updated_at) "
        "VALUES ('s-legacy', 1, ?, ?, 'verified', 1700000100.0, 1700000101.0)",
        (str(ws / "report.txt"), str(moved_dst)),
    )
    raw.execute(
        "INSERT INTO moves (session_id, seq, src, dst, state, created_at, updated_at) "
        "VALUES ('s-legacy', 2, ?, ?, 'verified', 1700000100.0, 1700000101.0)",
        (str(ws / "report_copy.txt"), str(staged_dup)),
    )
    raw.execute("PRAGMA user_version = 2")
    raw.commit()
    raw.close()

    with mock.patch.dict(os.environ, {"AUTOSORTER_APP_DIR": str(app_dir)}):
        # Opens & migrates sortify.db to v4 on MainThread while keeping WAL connection cached
        store = open_store(app_dir)
        user_ver = (
            get_db_connection(str(store.path)).execute("PRAGMA user_version").fetchone()
        )
        assert user_ver is not None and user_ver[0] == 4

        # Opens HistoryManager on DBWorker thread (triggers SessionCrypto.get_cipher probe)
        with open_session_history(store) as history:
            handoff = history.flush_staged_duplicates(
                "s-legacy",
                base_dir=str(ws),
                trashed_only=True,
                trash_fn=lambda p: os.remove(p),
            )
            assert len(handoff["exported"]) == 1
            restored_count = history.rollback("s-legacy")
            assert restored_count == 1

        summaries = list_sessions(store)
        assert summaries[0]["status"] == "rolled_back"
        assert summaries[0]["trashed_count"] == 1

    assert (ws / "report.txt").read_text(encoding="utf-8") == "report body"
    assert not moved_dst.exists()
    assert not staged_dup.exists()
