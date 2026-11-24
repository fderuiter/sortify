"""End-to-end tests for multi-folder watchdog daemon, shared user-scoped IPC, TUI controls,
quiet-window undo session batching, and reversible vs destructive Phase 0 compliance actions.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from textual.widgets import Button, Input, Static

from app.config import AppSettings
from app.core.daemon import ContinuousWatchdogDaemon
from app.core.ipc import get_ipc_dir, read_pid_file
from app.core.quarantine_interceptor import QuarantineInterceptorService
from app.core.services import acquire_services
from app.core.session_catalog import list_sessions
from app.core.store import open_store
from app.main import (
    build_parser,
    handle_config_command,
    handle_daemon_command,
    handle_undo_command,
)
from app.ui.a11y import inspect_tui_component
from app.ui.modals.settings import SettingsModal
from app.ui.tui import AutoSorterTUI

pytestmark = pytest.mark.xdist_group(name="tui")


@pytest.fixture(autouse=True)
def _isolated_app_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Provide an isolated app_store (IPC + sortify.db) and deterministic key per test."""
    app_dir = tmp_path / "app_store"
    app_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    monkeypatch.setenv("SORTIFY_DB_KEY", "a" * 64)


def _wait_until(predicate, timeout: float = 8.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def test_cli_and_tui_settings_persist_watched_folders_and_notify_daemon(tmp_path):
    """AC 1: Saving WATCHED_FOLDERS via CLI config and TUI SettingsModal persists paths and notifies daemon."""
    dir_a = tmp_path / "downloads"
    dir_b = tmp_path / "desktop"
    dir_c = tmp_path / "scanner_inbox"
    protected_dir = tmp_path / "protected_vault"
    for d in (dir_a, dir_b, dir_c, protected_dir):
        d.mkdir()

    cfg_path = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(cfg_path))
    settings._settings_model.AI_CONSENT_GRANTED = False
    settings.PROTECTED_PATHS = [str(protected_dir)]
    settings.flush()

    parser = build_parser()

    # 1. CLI --add-watch persists ordered folders
    args_add = parser.parse_args(
        ["config", "--add-watch", str(dir_a), "--add-watch", str(dir_b), "--json"]
    )
    with pytest.raises(SystemExit) as exc_info:
        handle_config_command(args_add, settings)
    assert exc_info.value.code == 0

    saved_json = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved_json["WATCHED_FOLDERS"] == [str(dir_a), str(dir_b)]

    # 2. CLI --remove-watch removes folder and notifies running daemon via IPC reload
    daemon = ContinuousWatchdogDaemon(
        settings, watched_folders=[str(dir_a), str(dir_b)]
    )
    daemon.start()
    try:
        assert _wait_until(lambda: read_pid_file() is not None, timeout=3.0)
        args_rem = parser.parse_args(["config", "--remove-watch", str(dir_a), "--json"])
        with pytest.raises(SystemExit) as exc_rem:
            handle_config_command(args_rem, settings)
        assert exc_rem.value.code == 0
        assert _wait_until(lambda: daemon.watched_folders == [str(dir_b)], timeout=3.0)
    finally:
        daemon.stop()

    # 3. TUI SettingsModal edits, validates, checks accessibility at 80x24, and saves WATCHED_FOLDERS
    async def _run_tui_settings():
        app = AutoSorterTUI(settings=settings, base_dir=str(dir_b), skip_wizard=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.action_open_settings()
            await pilot.pause()
            assert isinstance(app.screen, SettingsModal)
            modal = app.screen

            violations = inspect_tui_component(modal)
            assert violations == [], f"A11y errors on SettingsModal: {violations}"

            inp_watched = modal.query_one("#input-watched-folders", Input)
            assert inp_watched.value == str(dir_b)

            # Invalid overlap with protected path must fail validation and keep modal open
            inp_watched.value = f"{dir_b}, {protected_dir}"
            modal.action_save()
            await pilot.pause()
            assert isinstance(app.screen, SettingsModal)
            assert any(
                "overlaps protected path" in a["message"] for a in app.announcements
            )

            # Valid ordered watched folders save and persist to settings.json
            inp_watched.value = f"{dir_b}, {dir_c}, {dir_a}"
            modal.query_one("#btn-remove-watched-folder", Button).press()
            await pilot.pause()
            assert inp_watched.value == f"{dir_b}, {dir_c}"

            modal.action_save()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert not isinstance(app.screen, SettingsModal)
            assert settings.WATCHED_FOLDERS == [str(dir_b), str(dir_c)]

    asyncio.run(_run_tui_settings())
    persisted = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert persisted["WATCHED_FOLDERS"] == [str(dir_b), str(dir_c)]


def test_single_daemon_routes_multiple_folders_and_updates_watches_dynamically(
    tmp_path, capsys
):
    """AC 2: One daemon watches multiple roots, routes strictly per-root, and dynamically adds/removes watches."""
    inbox_a = tmp_path / "inbox_a"
    inbox_b = tmp_path / "inbox_b"
    inbox_c = tmp_path / "inbox_c"
    for d in (inbox_a, inbox_b, inbox_c):
        d.mkdir()

    cfg_path = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(cfg_path))
    settings._settings_model.AI_CONSENT_GRANTED = False
    settings.DEBOUNCE_DELAY = 0.2
    settings.MAX_DEBOUNCE_DELAY = 1.0
    settings.KEYWORD_RULES = {
        "invoice": "Finance/Invoices",
        "protocol": "Clinical/Protocols",
        "contract": "Legal/Contracts",
    }
    settings.WATCHED_FOLDERS = [str(inbox_a), str(inbox_b)]
    settings.flush()

    daemon = ContinuousWatchdogDaemon(settings)
    daemon.start()
    try:
        assert _wait_until(
            lambda: (Path(get_ipc_dir()) / ".autosorter.pid").exists(), timeout=3.0
        )
        # Lockfiles and sockets must live in user-scoped IPC dir, NOT watched roots
        for watched_root in (inbox_a, inbox_b, inbox_c):
            assert not (watched_root / ".autosorter.pid").exists()
            assert not (watched_root / ".autosorter.sock").exists()

        assert daemon.watched_folders == [str(inbox_a), str(inbox_b)]
        assert len(daemon._watch_handles) == 2

        # Drop files into inbox_a and inbox_b simultaneously
        file_a = inbox_a / "vendor_invoice_2026.txt"
        file_b = inbox_b / "study_protocol_v2.txt"
        file_a.write_text(
            "Quarterly vendor invoice statement due net 30.", encoding="utf-8"
        )
        file_b.write_text(
            "Clinical trial protocol synopsis and endpoints.", encoding="utf-8"
        )

        target_a = inbox_a / "Finance" / "Invoices" / "vendor_invoice_2026.txt"
        target_b = inbox_b / "Clinical" / "Protocols" / "study_protocol_v2.txt"
        assert _wait_until(
            lambda: target_a.is_file() and target_b.is_file(), timeout=20.0
        )
        assert not file_a.exists()
        assert not file_b.exists()
        # Strictly routed within each file's own watched root (no cross-folder contamination)
        assert not (inbox_a / "Clinical").exists()
        assert not (inbox_b / "Finance").exists()

        # Dynamically add inbox_c and remove inbox_b WITHOUT restarting the daemon
        settings.WATCHED_FOLDERS = [str(inbox_a), str(inbox_c)]
        settings.flush()
        assert _wait_until(
            lambda: daemon.watched_folders == [str(inbox_a), str(inbox_c)],
            timeout=3.0,
        )

        # File dropped into unscheduled inbox_b stays untouched; file in inbox_c is routed
        untouched_b = inbox_b / "unwatched_contract.txt"
        routed_c = inbox_c / "master_contract.txt"
        untouched_b.write_text(
            "Signed master service contract agreement.", encoding="utf-8"
        )
        routed_c.write_text(
            "Signed master service contract agreement.", encoding="utf-8"
        )

        target_c = inbox_c / "Legal" / "Contracts" / "master_contract.txt"
        assert _wait_until(lambda: target_c.is_file(), timeout=20.0)
        assert untouched_b.is_file()

        # CLI multi-folder status, folder-scoped pause/resume, and folder-scoped stop
        parser = build_parser()
        handle_daemon_command(parser.parse_args(["daemon", "status"]), settings)
        status_out, _ = capsys.readouterr()
        assert "Watched Folders (2)" in status_out
        assert str(inbox_a) in status_out
        assert str(inbox_c) in status_out

        handle_daemon_command(
            parser.parse_args(["daemon", "pause", str(inbox_a)]), settings
        )
        capsys.readouterr()
        assert str(inbox_a) in daemon._list_paused_folders()
        assert daemon.is_paused is False  # inbox_c is still active

        handle_daemon_command(
            parser.parse_args(["daemon", "resume", str(inbox_a)]), settings
        )
        capsys.readouterr()
        assert daemon._list_paused_folders() == []

        # Targeted stop on inbox_c unschedules inbox_c while keeping daemon running on inbox_a
        handle_daemon_command(
            parser.parse_args(["daemon", "stop", str(inbox_c)]), settings
        )
        stop_out, _ = capsys.readouterr()
        assert "updated" in stop_out.lower()
        assert daemon.is_running is True
        assert daemon.watched_folders == [str(inbox_a)]
    finally:
        daemon.stop()


def test_tui_polls_secondary_watched_folder_and_renders_live_telemetry(tmp_path):
    """AC 3: AutoSorterTUI._poll_daemon_status shows active state and queue/worker counts for secondary watched dirs."""
    dir_primary = tmp_path / "primary_ws"
    dir_secondary = tmp_path / "secondary_watched"
    dir_primary.mkdir()
    dir_secondary.mkdir()

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings._settings_model.AI_CONSENT_GRANTED = False
    settings.WATCHED_FOLDERS = [str(dir_secondary)]

    daemon = ContinuousWatchdogDaemon(settings, watched_folders=[str(dir_secondary)])
    daemon.start()
    try:
        assert _wait_until(lambda: read_pid_file() is not None, timeout=3.0)

        async def _run():
            app = AutoSorterTUI(
                settings=settings,
                source_dirs=[str(dir_primary), str(dir_secondary)],
                skip_wizard=True,
            )
            async with app.run_test(size=(120, 32)) as pilot:
                await app._poll_daemon_status()
                await pilot.pause()
                badge = app._get_daemon_status_badge()
                assert badge.startswith("[Daemon: Active | 1 dir | Q:")
                assert "| W:" in badge
                sb_text = str(app.query_one("#status-bar", Static).render())
                assert "[Daemon: Active | 1 dir |" in sb_text
                assert app._daemon_queue_depth == 0
                assert (app._daemon_active_workers or 0) >= 1

        asyncio.run(_run())
    finally:
        daemon.stop()


def test_tui_daemon_lifecycle_start_pause_resume_stop_controls(tmp_path):
    """AC 4: TUI daemon controls start an idle daemon, pause, resume, and stop cleanly."""
    dir_a = tmp_path / "watch_one"
    dir_b = tmp_path / "watch_two"
    dir_a.mkdir()
    dir_b.mkdir()

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings._settings_model.AI_CONSENT_GRANTED = False
    settings.WATCHED_FOLDERS = [str(dir_a), str(dir_b)]

    async def _run():
        app = AutoSorterTUI(
            settings=settings,
            source_dirs=[str(dir_a), str(dir_b)],
            skip_wizard=True,
        )
        async with app.run_test(size=(120, 32)) as pilot:
            await pilot.pause()
            assert app._daemon_state == "idle"
            assert "[Daemon: Idle]" in str(
                app.query_one("#status-bar", Static).render()
            )

            # 1. Start idle daemon via Ctrl+T
            await pilot.press("ctrl+t")
            for _ in range(30):
                if app._daemon_state == "active":
                    break
                await pilot.pause(0.05)
            assert app._daemon_state == "active"
            assert app._daemon_watched_folders == [str(dir_a), str(dir_b)]
            assert "[Daemon: Active | 2 dirs |" in app._get_daemon_status_badge()
            assert (Path(get_ipc_dir()) / ".autosorter.pid").exists()

            # 2. Pause via Ctrl+T
            await pilot.press("ctrl+t")
            for _ in range(20):
                if app._daemon_state == "paused":
                    break
                await pilot.pause(0.05)
            assert app._daemon_state == "paused"
            assert "[Daemon: Paused | 2 dirs |" in app._get_daemon_status_badge()

            # 3. Resume via 'p'
            await pilot.press("p")
            for _ in range(20):
                if app._daemon_state == "active":
                    break
                await pilot.pause(0.05)
            assert app._daemon_state == "active"

            # 4. Stop cleanly via Ctrl+Shift+T
            await pilot.press("ctrl+shift+t")
            for _ in range(30):
                if app._daemon_state == "idle":
                    break
                await pilot.pause(0.05)
            assert app._daemon_state == "idle"
            assert app._get_daemon_status_badge() == "[Daemon: Idle]"
            assert not (Path(get_ipc_dir()) / ".autosorter.pid").exists()

    asyncio.run(_run())


def test_quiet_window_burst_creates_single_session_and_undo_restores_all_three(
    tmp_path,
):
    """AC 5: Dropping 3 files during one quiet window creates 1 session and undo --latest restores all 3."""
    inbox = tmp_path / "burst_inbox"
    inbox.mkdir()

    cfg_path = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(cfg_path))
    settings._settings_model.AI_CONSENT_GRANTED = False
    settings.DEBOUNCE_DELAY = 0.45
    settings.MAX_DEBOUNCE_DELAY = 3.0
    settings.KEYWORD_RULES = {
        "invoice": "Finance",
        "receipt": "Expenses",
        "agenda": "Meetings",
    }
    settings.WATCHED_FOLDERS = [str(inbox)]
    settings.flush()

    daemon = ContinuousWatchdogDaemon(settings, watched_folders=[str(inbox)])
    daemon.start()
    try:
        f1 = inbox / "invoice_jan.txt"
        f2 = inbox / "receipt_hotel.txt"
        f3 = inbox / "agenda_board.txt"
        f1.write_text("Invoice total $1200 for consulting.", encoding="utf-8")
        f2.write_text("Hotel receipt reimbursement $340.", encoding="utf-8")
        f3.write_text("Quarterly board meeting agenda items.", encoding="utf-8")

        dst1 = inbox / "Finance" / "invoice_jan.txt"
        dst2 = inbox / "Expenses" / "receipt_hotel.txt"
        dst3 = inbox / "Meetings" / "agenda_board.txt"

        assert _wait_until(
            lambda: dst1.is_file() and dst2.is_file() and dst3.is_file(),
            timeout=20.0,
        )

        # Wait for the quiet-window timer (0.45s) to close the single shared session
        def _completed_burst_session() -> bool:
            sessions = [
                s
                for s in list_sessions()
                if s.get("base_dir") == str(inbox) and s.get("status") == "completed"
            ]
            return len(sessions) == 1 and sessions[0].get("file_count") == 3

        assert _wait_until(_completed_burst_session, timeout=12.0)
        inbox_sessions = [s for s in list_sessions() if s.get("base_dir") == str(inbox)]
        assert len(inbox_sessions) == 1
        assert inbox_sessions[0]["status"] == "completed"
        assert inbox_sessions[0]["file_count"] == 3
    finally:
        daemon.stop()

    # Run CLI undo --latest and verify all 3 files return to inbox
    parser = build_parser()
    undo_args = parser.parse_args(["undo", "--latest", "--json"])
    with pytest.raises(SystemExit) as exc_undo:
        handle_undo_command(undo_args, settings)
    assert exc_undo.value.code == 0

    assert f1.is_file()
    assert f2.is_file()
    assert f3.is_file()
    assert not dst1.exists()
    assert not dst2.exists()
    assert not dst3.exists()


def test_phase0_reversible_compliance_holds_and_irreversible_redact(tmp_path):
    """AC 6: archive, retain, quarantine, and sensitivity_hold roll back via MoveLedger; redact never does."""
    store = open_store(tmp_path / "compliance_store")
    with acquire_services(store) as services:
        db = services.db
        hm = services.history
        # 1. Unified store Phase 0 archive, retain, quarantine, and sensitivity_hold
        ws = tmp_path / "compliance_ws"
        ws.mkdir()

        f_archive = ws / "legacy_ledger.txt"
        f_retain = ws / "tax_record.txt"
        f_quarantine = ws / "suspicious_note.txt"
        f_sens = ws / "critical_clinical.txt"
        f_redact = ws / "patient_ssn.txt"

        f_archive.write_text("Archive retention payload 2020.", encoding="utf-8")
        f_retain.write_text("Tax retention payload 2024.", encoding="utf-8")
        f_quarantine.write_text("Flagged compliance review payload.", encoding="utf-8")
        f_sens.write_text(
            "Confidential Medical Report Subject 1042 Diagnosis: Hypertension",
            encoding="utf-8",
        )
        raw_pii_text = "Patient SSN: 123-45-6789 and email jane.doe@example.com"
        f_redact.write_text(raw_pii_text, encoding="utf-8")

        policies = [
            {
                "type": "keyword",
                "expression": "archive retention",
                "action": "archive",
                "target_path": "ArchiveVault",
                "priority": 100,
            },
            {
                "type": "keyword",
                "expression": "tax retention",
                "action": "retain",
                "target_path": "RetainedVault",
                "priority": 90,
            },
            {
                "type": "keyword",
                "expression": "flagged compliance",
                "action": "quarantine",
                "target_path": "QuarantineHold",
                "priority": 80,
            },
            {
                "type": "keyword",
                "expression": "patient",
                "action": "redact",
                "target_path": "RedactedOut",
                "priority": 95,
            },
        ]

        class _CritClassifier:
            def classify(
                self, file_path: str = "", text_content: str = "", **_kwargs
            ) -> dict:
                if "Subject 1042" in text_content:
                    return {
                        "sensitivity_rating": "CRITICAL",
                        "sensitivity_score": 0.99,
                        "category": "PHI",
                    }
                return {
                    "sensitivity_rating": "LOW",
                    "sensitivity_score": 0.1,
                    "category": "Uncategorized",
                }

        service = QuarantineInterceptorService(
            db=db,
            policies=policies,
            jev_classifier=_CritClassifier(),
            auto_quarantine_ratings=["CRITICAL"],
            history_manager=hm,
        )

        # Verify all 4 non-destructive holds record verified MoveLedger rows and roll back cleanly
        for src_file, action_hint in (
            (f_archive, "archive"),
            (f_retain, "retain"),
            (f_quarantine, "quarantine"),
            (f_sens, None),
        ):
            original_bytes = src_file.read_bytes()
            staged = service.stage_incoming_file(
                str(src_file), str(ws), policy_action=action_hint
            )
            rec = service.process_quarantine_job(staged["job_id"])
            assert rec["status"] in ("QUARANTINED", "ARCHIVED", "RELEASED")
            assert not src_file.exists()

            sid = rec.get("move_session_id")
            assert sid is not None
            moves = hm.moves.list_moves(sid)
            assert len(moves) == 1
            assert moves[0].state == "verified"
            assert moves[0].src == str(src_file)
            assert os.path.isfile(moves[0].dst)

            restored_count = hm.rollback(sid)
            assert restored_count == 1
            assert src_file.is_file()
            assert src_file.read_bytes() == original_bytes

            # Non-terminal QUARANTINED/ARCHIVED records transition to DISMISSED; terminal RELEASED stays RELEASED
            q_rec = db.get_quarantine_record(staged["job_id"])
            assert q_rec is not None
            expected_post_rollback = (
                "RELEASED" if action_hint == "retain" else "DISMISSED"
            )
            assert q_rec["status"] == expected_post_rollback

        # 2. Verify destructive `redact` scrubs PII, deletes unredacted source, and NEVER creates a reversible MoveLedger row
        sessions_before_redact = {s["session_id"] for s in hm.get_sessions()}
        staged_redact = service.stage_incoming_file(
            str(f_redact), str(ws), policy_action="redact"
        )
        rec_redact = service.process_quarantine_job(staged_redact["job_id"])
        assert rec_redact["status"] == "RELEASED"
        assert rec_redact["policy_action"] == "redact"
        assert rec_redact.get("move_session_id") is None
        assert not f_redact.exists()

        redacted_output = ws / "RedactedOut" / "patient_ssn.txt"
        assert redacted_output.is_file()
        redacted_content = redacted_output.read_text(encoding="utf-8")
        assert "123-45-6789" not in redacted_content
        assert "jane.doe@example.com" not in redacted_content
        assert "[REDACTED_" in redacted_content

        # Ensure no unredacted copy exists anywhere outside _Quarantine_Staging
        for path in ws.rglob("*"):
            if path.is_file() and "_Quarantine_Staging" not in path.parts:
                assert "123-45-6789" not in path.read_text(encoding="utf-8")

        sessions_after_redact = {s["session_id"] for s in hm.get_sessions()}
        assert sessions_after_redact == sessions_before_redact

    # 3. Also verify standalone custom --db-path (without HistoryManager) uses sidecar history.db cleanly
    standalone_db_path = tmp_path / "standalone_custom" / "sortify.db"
    standalone_db_path.parent.mkdir(parents=True, exist_ok=True)
    with acquire_services(db_path=standalone_db_path) as standalone_services:
        standalone_ws = tmp_path / "standalone_ws"
        standalone_ws.mkdir()
        standalone_src = standalone_ws / "standalone_hold.txt"
        standalone_src.write_text("flagged compliance standalone", encoding="utf-8")
        standalone_svc = QuarantineInterceptorService(
            db=standalone_services.db,
            policies=[
                {
                    "type": "keyword",
                    "expression": "flagged compliance",
                    "action": "quarantine",
                    "target_path": "QuarantineHold",
                    "priority": 80,
                }
            ],
        )
        st_job = standalone_svc.stage_incoming_file(
            str(standalone_src), str(standalone_ws), policy_action="quarantine"
        )
        st_rec = standalone_svc.process_quarantine_job(st_job["job_id"])
        assert st_rec["status"] == "QUARANTINED"
        assert st_rec.get("move_session_id") is not None
        assert (standalone_db_path.parent / "history.db").is_file()


def test_mixed_phase0_hold_and_normal_sort_burst_rolls_back_together(tmp_path):
    """Mixed quiet-window burst (Phase 0 compliance hold + SortPipeline move) shares 1 undo session."""
    inbox = tmp_path / "mixed_inbox"
    inbox.mkdir()

    cfg_path = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(cfg_path))
    settings._settings_model.AI_CONSENT_GRANTED = False
    settings.DEBOUNCE_DELAY = 0.45
    settings.MAX_DEBOUNCE_DELAY = 3.0
    settings.KEYWORD_RULES = {"xyz_sprint_tag": "Meetings"}
    settings.POLICIES = [
        {
            "type": "keyword",
            "expression": "board_hold_secret",
            "action": "quarantine",
            "target_path": "ComplianceHold",
            "priority": 100,
        }
    ]
    settings.WATCHED_FOLDERS = [str(inbox)]
    settings.flush()

    daemon = ContinuousWatchdogDaemon(settings, watched_folders=[str(inbox)])
    daemon.start()
    try:
        hold_file = inbox / "board_memo.txt"
        sort_file = inbox / "note_xyz.txt"
        hold_file.write_text("Confidential board_hold_secret memo.", encoding="utf-8")
        sort_file.write_text("Custom xyz_sprint_tag action item.", encoding="utf-8")

        sorted_dst = inbox / "Meetings" / "note_xyz.txt"
        assert _wait_until(
            lambda: sorted_dst.is_file() and not hold_file.exists(),
            timeout=20.0,
        )

        def _mixed_session_closed() -> bool:
            sessions = [
                s
                for s in list_sessions()
                if s.get("base_dir") == str(inbox) and s.get("status") == "completed"
            ]
            return len(sessions) == 1 and sessions[0].get("file_count") == 2

        assert _wait_until(_mixed_session_closed, timeout=12.0)
    finally:
        daemon.stop()

    parser = build_parser()
    with pytest.raises(SystemExit) as exc_undo:
        handle_undo_command(parser.parse_args(["undo", "--latest", "--json"]), settings)
    assert exc_undo.value.code == 0

    assert hold_file.is_file()
    assert sort_file.is_file()
    assert "board_hold_secret" in hold_file.read_text(encoding="utf-8")
    assert not sorted_dst.exists()


def test_multi_folder_daemon_preserves_rename_pattern_and_policy_rename_on_rollback(
    tmp_path,
):
    """Multi-folder daemon preserves RENAME_PATTERN config and #1193 policy rename_pattern across roots and undo."""
    inbox_fin = tmp_path / "inbox_finance"
    inbox_arch = tmp_path / "inbox_archive"
    inbox_fin.mkdir()
    inbox_arch.mkdir()

    cfg_path = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(cfg_path))
    settings._settings_model.AI_CONSENT_GRANTED = False
    settings.DEBOUNCE_DELAY = 0.4
    settings.MAX_DEBOUNCE_DELAY = 2.0
    settings.RENAME_PATTERN = "{category}_{stem}{extension}"
    settings.POLICIES = [
        {
            "type": "keyword",
            "expression": "retention_vault_doc",
            "action": "archive",
            "target_path": "Vault",
            "rename_pattern": "ARCH_{stem}{extension}",
            "priority": 100,
        },
        {
            "type": "keyword",
            "expression": "quarterly_tax_filing",
            "target_path": "Invoices",
            "rename_pattern": "INV_{stem}{extension}",
            "priority": 90,
        },
    ]
    settings.WATCHED_FOLDERS = [str(inbox_fin), str(inbox_arch)]
    settings.flush()

    reloaded = AppSettings(filepath=str(cfg_path))
    assert reloaded.RENAME_PATTERN == "{category}_{stem}{extension}"
    assert reloaded.WATCHED_FOLDERS == [str(inbox_fin), str(inbox_arch)]

    daemon = ContinuousWatchdogDaemon(settings)
    daemon.start()
    try:
        fin_src = inbox_fin / "q4_bill.txt"
        arch_src = inbox_arch / "ledger_record.txt"
        fin_src.write_text("Annual quarterly_tax_filing balance.", encoding="utf-8")
        arch_src.write_text("Official retention_vault_doc payload.", encoding="utf-8")

        fin_dst = inbox_fin / "Invoices" / "INV_q4_bill.txt"
        arch_dst = inbox_arch / "Vault" / "ARCH_ledger_record.txt"
        assert _wait_until(
            lambda: fin_dst.is_file() and arch_dst.is_file(),
            timeout=20.0,
        )

        def _both_sessions_closed() -> bool:
            closed = [
                s
                for s in list_sessions()
                if s.get("base_dir") in {str(inbox_fin), str(inbox_arch)}
                and s.get("status") == "completed"
            ]
            return len(closed) == 2

        assert _wait_until(_both_sessions_closed, timeout=12.0)
    finally:
        daemon.stop()

    # Roll back both completed per-folder sessions and verify original filenames are restored
    parser = build_parser()
    for _ in range(2):
        with pytest.raises(SystemExit) as exc_undo:
            handle_undo_command(
                parser.parse_args(["undo", "--latest", "--json"]), settings
            )
        assert exc_undo.value.code == 0

    assert fin_src.is_file()
    assert arch_src.is_file()
    assert not fin_dst.exists()
    assert not arch_dst.exists()
