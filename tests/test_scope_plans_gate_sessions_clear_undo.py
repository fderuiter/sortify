"""Regression tests for scoped plans, gated sessions, and cleared undo targets."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from textual.widgets import Tree

from app.config import AppSettings
from app.core.db_conn import get_db_connection
from app.core.db_worker import DBWorker
from app.core.moves import INTENT
from app.core.session_catalog import list_sessions
from app.core.store import open_store
from app.ui.tui import AutoSorterTUI, RollbackConfirmModal
from tests.test_cli_subcommands import run_cli


def _write_app_settings(
    app_dir: Path,
    *,
    max_depth: int = 5,
    preserve_hierarchy: bool = False,
    keyword_rules: dict[str, str] | None = None,
    conflict_policy: str = "rename",
) -> AppSettings:
    """Write an isolated settings.json inside ``app_dir`` for CLI or TUI test runs."""
    app_dir.mkdir(parents=True, exist_ok=True)
    settings = AppSettings(filepath=str(app_dir / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    settings.SCAN_MAX_DEPTH = max_depth
    settings.PRESERVE_HIERARCHY = preserve_hierarchy
    settings.CONFLICT_POLICY = conflict_policy
    settings.CONTEXTUAL_RENAMING = False
    settings.CLINICAL_SMART_RENAMING = False
    if keyword_rules is not None:
        settings.KEYWORD_RULES = keyword_rules
    settings.flush()
    return settings


def _collect_tree_labels(node) -> list[str]:
    """Collect plain-text labels from all descendants of a Textual Tree node."""
    labels: list[str] = []
    for child in node.children:
        label_plain = (
            child.label.plain if hasattr(child.label, "plain") else str(child.label)
        )
        labels.append(label_plain)
        labels.extend(_collect_tree_labels(child))
    return labels


async def _wait_until(predicate, pilot, timeout_steps: int = 120) -> bool:
    """Poll ``predicate`` across Textual pilot pauses until truthy or timed out."""
    for _ in range(timeout_steps):
        if predicate():
            return True
        await pilot.pause(0.05)
    return bool(predicate())


@pytest.mark.parametrize("max_depth", [0, 5])
def test_repeat_sort_and_scan_are_idempotent_at_depth_0_and_5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, max_depth: int
) -> None:
    """Repeat sort -> scan and sort -> sort print Nothing to sort and move 0 files."""
    app_dir = tmp_path / f"app_depth_{max_depth}"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    _write_app_settings(
        app_dir,
        max_depth=max_depth,
        keyword_rules={"invoice": "Invoices"},
    )

    ws = tmp_path / f"ws_{max_depth}"
    ws.mkdir()
    (ws / "invoice_q1.txt").write_text("Quarterly vendor invoice total $450.00")
    (ws / "archive1.zip").write_bytes(b"PK\x03\x04dummyzip")

    # 1. Initial sort moves both files and records one completed session.
    code1, _out1, err1 = run_cli(["sort", str(ws)])
    assert code1 == 0, err1
    assert (ws / "Invoices" / "invoice_q1.txt").exists()
    assert (ws / "Archives" / "archive1.zip").exists()
    assert not (ws / "invoice_q1.txt").exists()
    assert not (ws / "archive1.zip").exists()

    sessions_after_first = list_sessions(open_store(app_dir))
    completed_first = [s for s in sessions_after_first if s["status"] == "completed"]
    assert len(completed_first) == 1
    assert completed_first[0]["step_count"] == 2

    # 2. Follow-up scan reports Nothing to sort and does not replan moved files.
    code_scan, out_scan, err_scan = run_cli(["scan", str(ws)])
    assert code_scan == 0, err_scan
    assert "Nothing to sort" in err_scan
    assert out_scan.strip() == ""

    # 3. Follow-up sort prints Nothing to sort, moves 0 files, and records 0 sessions.
    code2, out2, err2 = run_cli(["sort", str(ws)])
    assert code2 == 0, err2
    assert "Nothing to sort" in err2
    assert "To undo this sort" not in err2
    assert out2.strip() == ""
    assert (ws / "Invoices" / "invoice_q1.txt").exists()
    assert (ws / "Archives" / "archive1.zip").exists()
    assert not (ws / "Archives" / "Archives").exists()

    sessions_after_second = list_sessions(open_store(app_dir))
    completed_second = [s for s in sessions_after_second if s["status"] == "completed"]
    assert len(completed_second) == 1
    assert completed_second[0]["session_id"] == completed_first[0]["session_id"]


def test_preserve_hierarchy_repeat_cli_sort_does_not_nest_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeat sort with PRESERVE_HIERARCHY=True keeps Archives/archive1.zip in place."""
    app_dir = tmp_path / "app_hier"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    _write_app_settings(
        app_dir,
        max_depth=5,
        preserve_hierarchy=True,
        keyword_rules={"invoice": "Finance/2025"},
    )

    ws = tmp_path / "ws_hier"
    ws.mkdir()
    (ws / "archive1.zip").write_bytes(b"PK\x03\x04zipbytes")
    (ws / "invoice_jan.txt").write_text("January 2025 consulting invoice")

    code1, _out1, err1 = run_cli(["sort", str(ws)])
    assert code1 == 0, err1
    assert (ws / "Archives" / "archive1.zip").exists()
    assert (ws / "Finance" / "2025" / "invoice_jan.txt").exists()

    code2, out2, err2 = run_cli(["sort", str(ws)])
    assert code2 == 0, err2
    assert "Nothing to sort" in err2
    assert out2.strip() == ""
    assert (ws / "Archives" / "archive1.zip").exists()
    assert not (ws / "Archives" / "Archives").exists()
    assert (ws / "Finance" / "2025" / "invoice_jan.txt").exists()
    assert not (ws / "Finance" / "2025" / "Finance").exists()


def test_tui_preserve_hierarchy_consecutive_ctrl_e_and_ctrl_z_after_zero_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consecutive Ctrl+E in AutoSorterTUI clears #plan-tree, avoids Archives/Archives, and Ctrl+Z targets real sort."""
    app_dir = tmp_path / "app_tui"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    monkeypatch.setattr(
        "app.core.session_catalog.find_abandoned_sessions", lambda *a, **k: []
    )

    settings = _write_app_settings(
        app_dir,
        max_depth=5,
        preserve_hierarchy=True,
        keyword_rules={"invoice": "Invoices"},
    )

    ws = tmp_path / "ws_tui"
    ws.mkdir()
    (ws / "archive1.zip").write_bytes(b"PK\x03\x04zipbytes")
    (ws / "invoice_tui.txt").write_text("Invoice #99 for cloud hosting")

    # Seed a stale pre-existing 0-step completed session to verify Ctrl+Z skips it too.
    store = open_store(app_dir)
    conn = get_db_connection(str(store.path))
    with conn:
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
            ("legacy-zero-step", 9999999999.0, str(ws), "completed"),
        )

    async def _exercise_tui() -> None:
        def _workers_idle(tui_app: AutoSorterTUI) -> bool:
            return not any(w.is_running for w in tui_app.workers)

        app = AutoSorterTUI(settings=settings, base_dir=str(ws), skip_wizard=True)
        async with app.run_test(size=(120, 36)) as pilot:
            app.action_scan_directory()
            scan_done = await _wait_until(
                lambda: _workers_idle(app) and bool(app.plan),
                pilot,
            )
            assert scan_done, f"Initial scan did not populate plan: {app.plan}"

            plan_tree = app.query_one("#plan-tree", Tree)
            initial_labels = " ".join(_collect_tree_labels(plan_tree.root))
            assert "archive1.zip" in initial_labels

            # First Ctrl+E moves files and triggers post-execute rescan.
            app.action_execute_sort()
            exec_and_rescan_done = await _wait_until(
                lambda: (
                    (ws / "Archives" / "archive1.zip").exists()
                    and _workers_idle(app)
                    and not bool(app.plan)
                ),
                pilot,
            )
            assert exec_and_rescan_done, f"Post-Ctrl+E rescan left plan: {app.plan}"
            await pilot.pause(0.1)

            assert (ws / "Archives" / "archive1.zip").exists()
            assert (ws / "Invoices" / "invoice_tui.txt").exists()
            assert not (ws / "Archives" / "Archives").exists()

            cleared_labels = " ".join(_collect_tree_labels(plan_tree.root))
            assert "archive1.zip" not in cleared_labels
            assert "invoice_tui.txt" not in cleared_labels

            # Dismiss post-sort AuditReportModal if shown so main screen receives Ctrl+E / Ctrl+Z
            while len(app.screen_stack) > 1:
                app.pop_screen()
                await pilot.pause(0.05)

            # Consecutive Ctrl+E after rescan does not nest Archives/Archives or create a completed session.
            await pilot.press("ctrl+e")
            await pilot.pause(0.15)
            await _wait_until(lambda: _workers_idle(app), pilot)
            assert not (ws / "Archives" / "Archives").exists()

            all_sess = list_sessions(open_store(app_dir))
            real_completed = [
                s
                for s in all_sess
                if s["status"] == "completed" and s["step_count"] > 0
            ]
            assert len(real_completed) == 1
            real_session_id = real_completed[0]["session_id"]

            # Pressing Ctrl+Z skips the 0-step completed row (even with higher timestamp)
            # and targets the real session with MoveLedger entries.
            await pilot.press("ctrl+z")
            got_modal = await _wait_until(
                lambda: isinstance(app.screen, RollbackConfirmModal), pilot
            )
            modal = app.screen
            assert got_modal and isinstance(modal, RollbackConfirmModal)
            assert modal.session_info["session_id"] == real_session_id

            modal.dismiss(
                {
                    "confirmed": True,
                    "session_id": real_session_id,
                    "ignore_missing": False,
                    "selected_step_ids": None,
                }
            )
            restored_done = await _wait_until(
                lambda: (
                    (ws / "archive1.zip").exists()
                    and (ws / "invoice_tui.txt").exists()
                    and _workers_idle(app)
                ),
                pilot,
            )
            assert restored_done

    asyncio.run(_exercise_tui())


def test_sort_then_zero_move_sort_then_undo_latest_restores_first_sort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sort -> zero-move sort (both empty and skip-collision) -> undo --latest restores first sort."""
    app_dir = tmp_path / "app_undo_gate"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    _write_app_settings(
        app_dir,
        max_depth=0,
        keyword_rules={"invoice": "Invoices"},
        conflict_policy="skip",
    )

    ws1 = tmp_path / "ws1"
    ws1.mkdir()
    (ws1 / "invoice_a.txt").write_text("First sort invoice A")
    (ws1 / "invoice_b.txt").write_text("First sort invoice B")

    code1, _out1, err1 = run_cli(["sort", str(ws1)])
    assert code1 == 0, err1
    assert (ws1 / "Invoices" / "invoice_a.txt").exists()
    assert (ws1 / "Invoices" / "invoice_b.txt").exists()

    # Zero-move repeat sort on ws1
    code2, _out2, err2 = run_cli(["sort", str(ws1)])
    assert code2 == 0, err2
    assert "Nothing to sort" in err2

    # Collision-skipped zero-move sort on ws2 (candidate move exists in plan, but CONFLICT_POLICY='skip' skips it)
    ws2 = tmp_path / "ws2"
    (ws2 / "Invoices").mkdir(parents=True)
    (ws2 / "invoice_dup.txt").write_text("Source copy")
    (ws2 / "Invoices" / "invoice_dup.txt").write_text("Existing destination copy")

    code3, out3, err3 = run_cli(["sort", str(ws2)])
    assert code3 == 0, err3
    assert "Nothing to sort" in err3
    assert "To undo this sort" not in err3
    assert out3.strip() == ""
    assert (ws2 / "invoice_dup.txt").read_text() == "Source copy"

    # Zero-move runs must record zero completed sessions.
    sessions = list_sessions(open_store(app_dir))
    completed_sessions = [s for s in sessions if s["status"] == "completed"]
    assert len(completed_sessions) == 1
    assert completed_sessions[0]["step_count"] == 2

    # undo --latest restores ws1's files cleanly.
    code_undo, out_undo, err_undo = run_cli(["undo", "--latest", "--json"])
    assert code_undo == 0, err_undo
    assert json.loads(out_undo)["session_id"] == completed_sessions[0]["session_id"]
    assert (ws1 / "invoice_a.txt").exists()
    assert (ws1 / "invoice_b.txt").exists()
    assert not (ws1 / "Invoices" / "invoice_a.txt").exists()
    assert not (ws1 / "Invoices" / "invoice_b.txt").exists()


@pytest.mark.parametrize("rule_target", ["Invoices", "Finance/2025"])
def test_sort_undo_sort_clears_verified_target_and_avoids_unnamed_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rule_target: str
) -> None:
    """sort -> undo --latest -> sort with single or nested rules produces zero Unnamed_safe folders."""
    slug = rule_target.replace("/", "_")
    app_dir = tmp_path / f"app_roundtrip_{slug}"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    _write_app_settings(
        app_dir,
        max_depth=5,
        keyword_rules={"invoice": rule_target},
    )

    ws = tmp_path / f"ws_{slug}"
    ws.mkdir()
    (ws / "invoice_001.txt").write_text("Annual software license invoice 2025")
    (ws / "bundle.zip").write_bytes(b"PK\x03\x04archive")

    # 1. First sort
    code1, _out1, err1 = run_cli(["sort", str(ws)])
    assert code1 == 0, err1
    assert (ws / Path(rule_target) / "invoice_001.txt").exists()
    assert (ws / "Archives" / "bundle.zip").exists()
    assert not (ws / "Unnamed_safe").exists()

    # 2. Undo --latest clears user_verified_target_path to NULL rather than ""
    code_u, _out_u, err_u = run_cli(["undo", "--latest"])
    assert code_u == 0, err_u
    assert (ws / "invoice_001.txt").exists()
    assert (ws / "bundle.zip").exists()

    store = open_store(app_dir)
    conn = get_db_connection(str(store.path))
    with conn:
        rows = conn.execute(
            "SELECT filepath, user_verified_target_path FROM documents WHERE base_dir = ?",
            (str(ws),),
        ).fetchall()
    assert rows, "Expected document rows after sort + undo"
    for fp, verified in rows:
        assert verified is None, (
            f"Expected NULL verified target for {fp}, got {verified!r}"
        )

    # 3. Second sort routes cleanly with zero Unnamed_safe folders (even if nested rule changes)
    next_target = "Accounting/2025" if "/" in rule_target else rule_target
    _write_app_settings(
        app_dir,
        max_depth=5,
        keyword_rules={"invoice": next_target},
    )
    code2, _out2, err2 = run_cli(["sort", str(ws)])
    assert code2 == 0, err2
    assert (ws / Path(next_target) / "invoice_001.txt").exists()
    assert (ws / "Archives" / "bundle.zip").exists()
    assert not (ws / "Unnamed_safe").exists()


def test_preexisting_blank_targets_zero_step_sessions_and_reconciliation_safety(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-existing user_verified_target_path='' or step_count=0 rows do not create Unnamed_safe or block undo, and PRAGMA user_version is unchanged."""
    app_dir = tmp_path / "app_legacy_rows"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    store = open_store(app_dir)
    worker = DBWorker()
    try:
        db = store.database(worker)
        history = store.history(db, store.cache(worker))
        conn = get_db_connection(str(store.path))
        with conn:
            baseline_user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert baseline_user_version == 4

        ws = tmp_path / "ws_legacy"
        ws.mkdir()
        src_file = ws / "notes_alpha.txt"
        src_file.write_text("Project architecture meeting notes and action items")
        inv_file = ws / "invoice_legacy.txt"
        inv_file.write_text("Vendor invoice #404")

        db.upsert_document(
            str(ws),
            "notes_alpha.txt",
            "hash_notes",
            "Project architecture meeting notes and action items",
        )
        db.upsert_document(
            str(ws),
            "invoice_legacy.txt",
            "hash_inv",
            "Vendor invoice #404",
        )
        worker.q.join()

        # Simulate aborted move reconciliation on a root-level source file: must clear to NULL, not ""
        aborted_sid = history.start_session(str(ws))
        seq = history.moves.record_intent(
            aborted_sid,
            str(src_file),
            str(ws / "AbortedFolder" / "notes_alpha.txt"),
            src_sha256="hash_notes",
        )
        assert seq == 1
        assert history.moves.list_moves(aborted_sid)[0].state == INTENT
        history.moves.reconcile_incomplete_transactions(db=db)
        history._transition_session_status(aborted_sid, "abandoned")
        worker.q.join()

        with conn:
            reconciled_target = conn.execute(
                "SELECT user_verified_target_path FROM documents WHERE base_dir = ? AND filepath = ?",
                (str(ws), "notes_alpha.txt"),
            ).fetchone()[0]
        assert reconciled_target is None

        # Force pre-existing polluted SQLite state directly via raw SQL:
        # 1) user_verified_target_path = ""
        # 2) completed session with 0 moves (step_count = 0) and newer timestamp
        with conn:
            conn.execute(
                "UPDATE documents SET user_verified_target_path = '' WHERE base_dir = ?",
                (str(ws),),
            )
            conn.execute(
                "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
                ("polluted-empty-session", 9999999999.0, str(ws), "completed"),
            )
    finally:
        worker.stop()

    _write_app_settings(
        app_dir,
        max_depth=5,
        keyword_rules={"invoice": "Invoices"},
    )

    code_sort, _out_sort, err_sort = run_cli(["sort", str(ws)])
    assert code_sort == 0, err_sort
    assert not (ws / "Unnamed_safe").exists()
    assert (ws / "Invoices" / "invoice_legacy.txt").exists()

    # undo --latest must ignore 'polluted-empty-session' (step_count == 0) and restore ws_legacy!
    code_undo, out_undo, err_undo = run_cli(["undo", "--latest", "--json"])
    assert code_undo == 0, err_undo
    undo_payload = json.loads(out_undo)
    assert undo_payload["session_id"] != "polluted-empty-session"
    assert (ws / "notes_alpha.txt").exists()
    assert (ws / "invoice_legacy.txt").exists()

    conn_after = get_db_connection(str(open_store(app_dir).path))
    with conn_after:
        assert (
            conn_after.execute("PRAGMA user_version").fetchone()[0]
            == baseline_user_version
        )
