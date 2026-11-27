"""Tests for consolidating ContinuousWatchdogDaemon onto one in-process pipeline lane."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from watchdog.events import FileCreatedEvent, FileModifiedEvent

from app.config import AppSettings
from app.core.daemon import (
    ContinuousWatchdogDaemon,
    DaemonFolderHandler,
    DaemonState,
    FileChangeEvent,
    _run_sorting_process_entry,
)
from app.core.db import get_db_connection
from app.core.db_conn import clear_connection_cache
from app.core.folder_lock import FolderLock
from app.core.ipc import DaemonIPCClient, DaemonIPCServer
from app.core.jev_classifier import JevClassificationResult
from app.core.metadata import MetadataPass
from app.core.pipeline import IllegalStateTransitionError, SortPipeline
from app.core.quarantine_interceptor import QuarantineInterceptorService
from app.core.session import AppSession


def _make_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppSettings:
    app_dir = tmp_path / "app_dir"
    app_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    cfg_path = app_dir / "settings.json"
    settings = AppSettings(filepath=str(cfg_path))
    settings.AI_CONSENT_GRANTED = False
    settings.DEDUP_WINDOW = 0.01
    settings.MAX_DEBOUNCE_DELAY = 1.0
    settings.DEBOUNCE_DELAY = 0.05
    settings.RECONCILIATION_INTERVAL = 60.0
    settings._save()
    return settings


def test_external_watchdog_events_during_is_moving_enqueue_and_leave_zero_stranded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """External Watchdog events during is_moving enqueue into _event_queue and leave 0 stranded files."""
    settings = _make_settings(tmp_path, monkeypatch)
    settings.KEYWORD_RULES = {
        "invoice": "Invoices",
        "contract": "Contracts",
        "memo": "Memos",
    }
    settings._save()

    ws = tmp_path / "watched_ws"
    ws.mkdir()

    daemon = ContinuousWatchdogDaemon(settings, str(ws))
    try:
        daemon.start()
        assert daemon._event_queue is not None
        enqueued_during_moving: list[str] = []
        orig_put_nowait = daemon._event_queue.put_nowait

        def _spy_put_nowait(item: object) -> None:
            if daemon.is_moving and isinstance(item, tuple) and len(item) == 3:
                change_evt = item[2]
                if isinstance(change_evt, FileChangeEvent):
                    enqueued_during_moving.append(os.path.abspath(change_evt.file_path))
            orig_put_nowait(item)

        monkeypatch.setattr(daemon._event_queue, "put_nowait", _spy_put_nowait)

        handler = DaemonFolderHandler(daemon)
        external_files: list[Path] = []
        baseline_enqueued = daemon._events_enqueued_count

        # Hold worker consumption during the outer move scope so exiting the outer
        # scoped_move_phase deterministically drops _move_ref_count to 0 instead of
        # racing a worker's nested scoped_move_phase or delayed OS FSEvents.
        daemon.pause()

        with daemon.scoped_move_phase(plan={"seed.txt": "Invoices/seed.txt"}):
            assert daemon.is_moving is True

            # Self-generated source & destination events are suppressed
            handler.on_any_event(FileModifiedEvent(str(ws / "seed.txt")))
            handler.on_any_event(FileModifiedEvent(str(ws / "Invoices" / "seed.txt")))
            assert daemon.pending_dirty is False

            # External Watchdog events arrive while daemon.is_moving is True
            for idx, stem in enumerate(
                ("invoice_ext", "contract_ext", "memo_ext", "invoice_late")
            ):
                ext_file = ws / f"{stem}_{idx}.txt"
                ext_file.write_text(f"External {stem} payload {idx}", encoding="utf-8")
                external_files.append(ext_file)
                handler.on_any_event(FileCreatedEvent(str(ext_file)))

            assert daemon.pending_dirty is True
            expected_paths = {os.path.abspath(str(p)) for p in external_files}
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if expected_paths.issubset(set(enqueued_during_moving)):
                    break
                time.sleep(0.02)
            assert expected_paths.issubset(set(enqueued_during_moving))
            assert str(ws / "seed.txt") not in enqueued_during_moving
            assert daemon._events_enqueued_count - baseline_enqueued >= 4

        # Exiting scoped_move_phase on live _event_loop clears pending_dirty and schedules
        # reconciliation audit instead of spawning _sorting_process or starting _debounce_timer.
        assert daemon.is_moving is False
        assert daemon.pending_dirty is False
        assert daemon._sorting_process is None
        assert daemon._debounce_timer is None

        daemon.resume()

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if not [p for p in external_files if p.exists()] and not daemon.is_moving:
                break
            time.sleep(0.05)

        assert [p for p in external_files if p.exists()] == []
        assert daemon.pending_dirty is False
        assert daemon._sorting_process is None
        assert daemon._debounce_timer is None
        assert (ws / "Invoices" / "invoice_ext_0.txt").is_file()
        assert (ws / "Contracts" / "contract_ext_1.txt").is_file()
        assert (ws / "Memos" / "memo_ext_2.txt").is_file()
        assert (ws / "Invoices" / "invoice_late_3.txt").is_file()
    finally:
        daemon.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_trigger_recalculation_routes_to_sort_executor_when_event_loop_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When _event_loop is active, trigger_recalculation runs on _sort_executor without spawning a subprocess."""
    settings = _make_settings(tmp_path, monkeypatch)
    settings.KEYWORD_RULES = {"invoice": "Invoices"}
    settings._save()

    ws = tmp_path / "recalc_ws"
    ws.mkdir()

    daemon = ContinuousWatchdogDaemon(settings, str(ws))
    observed_threads: list[str] = []
    first_started = threading.Event()
    release_first = threading.Event()
    orig_sync = daemon._run_sorting_sync

    def _wrapped_sync(cancel_event: threading.Event, **kwargs: Any) -> None:
        observed_threads.append(threading.current_thread().name)
        if not first_started.is_set():
            first_started.set()
            release_first.wait(timeout=3.0)
        orig_sync(cancel_event, **kwargs)

    monkeypatch.setattr(daemon, "_run_sorting_sync", _wrapped_sync)

    try:
        daemon.start()
        (ws / "invoice_batch.txt").write_text("Invoice 500", encoding="utf-8")
        daemon.trigger_recalculation()
        assert first_started.wait(timeout=3.0)

        # Queue a follow-up run while _recalc_in_progress is True
        daemon._schedule_run(threading.Event())
        assert daemon._rerun_pending is True
        assert daemon._sorting_process is None

        release_first.set()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if (
                len(observed_threads) >= 2
                and daemon.state == DaemonState.RUNNING
                and (ws / "Invoices" / "invoice_batch.txt").is_file()
            ):
                break
            time.sleep(0.05)

        assert len(observed_threads) >= 2
        assert all(name.startswith("daemon-sort") for name in observed_threads)
        assert daemon._sorting_process is None
        assert (ws / "Invoices" / "invoice_batch.txt").is_file()
    finally:
        release_first.set()
        daemon.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_standalone_subprocess_entry_gates_on_folder_lock_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Standalone _run_sorting_process_entry gates behind FolderLock and exits non-zero on unhandled errors."""
    settings = _make_settings(tmp_path, monkeypatch)
    settings.KEYWORD_RULES = {"invoice": "Invoices"}
    settings._save()

    ws = tmp_path / "standalone_ws"
    ws.mkdir()
    doc = ws / "invoice_1.txt"
    doc.write_text("Invoice standalone", encoding="utf-8")

    # 1. Busy FolderLock causes non-zero exit (code 1) and leaves file untouched
    monkeypatch.setattr(AppSession, "folder_lock_timeout", 0.05)
    with FolderLock(str(ws), timeout=0.0):
        with pytest.raises(SystemExit) as busy_exc:
            _run_sorting_process_entry(str(ws), str(settings._filepath))
        assert busy_exc.value.code == 1
        assert doc.is_file()

    # 2. Unhandled error in _run_sorting_sync exits non-zero (code 1)
    with mock.patch.object(
        ContinuousWatchdogDaemon,
        "_build_pipeline",
        side_effect=RuntimeError("boom in standalone worker"),
    ):
        with pytest.raises(SystemExit) as crash_exc:
            _run_sorting_process_entry(str(ws), str(settings._filepath))
        assert crash_exc.value.code == 1

    # 3. Unlocked run succeeds under FolderLock without nested self-conflict
    try:
        _run_sorting_process_entry(str(ws), str(settings._filepath))
        assert (ws / "Invoices" / "invoice_1.txt").is_file()
    finally:
        clear_connection_cache(only_current_and_inactive=False)


def test_second_app_session_leaves_active_intent_rows_untouched_when_folder_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening a second AppSession while FolderLock is held leaves active intent rows untouched."""
    settings = _make_settings(tmp_path, monkeypatch)
    ws = tmp_path / "locked_reconcile_ws"
    ws.mkdir()
    src_file = ws / "active_move.txt"
    payload = b"in-flight move payload"
    src_file.write_bytes(payload)
    src_hash = hashlib.sha256(payload).hexdigest()
    dst_file = ws / "Sorted" / "active_move.txt"

    session1 = AppSession(settings, base_dir=str(ws))
    session2 = None
    try:
        sid = session1.history_manager.start_session(str(ws))
        with FolderLock(str(ws), timeout=0.0):
            seq = session1.history_manager.moves.record_intent(
                sid,
                str(src_file),
                str(dst_file),
                src_sha256=src_hash,
            )

            # Opening a second AppSession triggers startup reconcile_incomplete_transactions
            session2 = AppSession(settings, base_dir=str(ws))

            moves = session1.history_manager.moves.list_moves(sid)
            assert len(moves) == 1
            assert moves[0].state == "intent"

            # Now complete the physical move and verify complete_moves succeeds with 0 ValueError
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            src_file.replace(dst_file)
            session1.history_manager.moves.complete_moves(
                sid,
                [
                    {
                        "seq": seq,
                        "state": "verified",
                        "dst": str(dst_file),
                        "displaced": None,
                    }
                ],
            )
            verified_moves = session1.history_manager.moves.list_moves(sid)
            assert verified_moves[0].state == "verified"

        # Once FolderLock is released, a genuinely stale intent row IS reconciled
        stale_src = ws / "stale.txt"
        stale_src.write_text("leftover", encoding="utf-8")
        stale_sid = session1.history_manager.start_session(str(ws))
        session1.history_manager.moves.record_intent(
            stale_sid,
            str(stale_src),
            str(ws / "Sorted" / "stale.txt"),
        )
        reconciled = session1.history_manager.moves.reconcile_incomplete_transactions(
            db=session1.db
        )
        assert reconciled == 1
        assert session1.history_manager.moves.list_moves(stale_sid)[0].state == "failed"
    finally:
        if session2 is not None:
            session2.close()
        session1.close()
        clear_connection_cache(only_current_and_inactive=False)


def test_phase0_release_removes_transient_row_and_commits_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Releasing a file from Phase 0 deletes the transient quarantine_records row and leaves in_transaction == False."""
    settings = _make_settings(tmp_path, monkeypatch)
    ws = tmp_path / "phase0_ws"
    ws.mkdir()
    sample = ws / "invoice_clean.txt"
    sample.write_text("Standard invoice #42", encoding="utf-8")

    session = AppSession(settings, base_dir=str(ws))
    try:
        interceptor = QuarantineInterceptorService(
            db=session.db,
            policies=[
                {
                    "type": "keyword",
                    "expression": "redact_only",
                    "action": "redact",
                    "target_path": "Redacted",
                }
            ],
            runtime_settings=settings,
        )
        staged = interceptor.stage_incoming_file(
            source_path=str(sample),
            base_dir=str(ws),
            original_relative_path="invoice_clean.txt",
        )
        job_id = staged["job_id"]

        result = interceptor._process_phase0_job(job_id)
        assert result.status == "RELEASED"
        assert session.db.get_quarantine_record(job_id) is None

        in_tx = session.db.worker.execute_write(
            lambda: get_db_connection(session.db.db_path).in_transaction
        )
        assert in_tx is False
    finally:
        session.close()
        clear_connection_cache(only_current_and_inactive=False)


@pytest.mark.anyio
async def test_phase1_fast_path_bypass_and_content_policy_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 1 bypasses static matches under non-content policies and skips fast_path_only under content policies."""
    settings = _make_settings(tmp_path, monkeypatch)
    settings.KEYWORD_RULES = {"invoice": "Invoices"}
    settings.POLICIES = [
        {
            "type": "pattern",
            "expression": "*.bak",
            "action": "archive",
            "target_path": "Backups",
            "priority": 50,
        }
    ]
    settings._save()

    ws = tmp_path / "fast_path_ws"
    ws.mkdir()
    inv = ws / "invoice_fast.txt"
    inv.write_text("Invoice content", encoding="utf-8")

    session = AppSession(settings, base_dir=str(ws))
    try:
        bypassed = MetadataPass.run(
            str(ws),
            ["invoice_fast.txt"],
            settings,
            session.db,
        )
        assert bypassed == ["invoice_fast.txt"]
    finally:
        session.close()

    # Now enable a content-inspecting redact policy and verify _triage_file_path skips Phase 1 (fast_path_only=True)
    settings.POLICIES = [
        {
            "type": "keyword",
            "expression": "ssn_token",
            "action": "redact",
            "target_path": "Redacted",
            "priority": 100,
        }
    ]
    settings._save()

    daemon = ContinuousWatchdogDaemon(settings, str(ws))
    daemon._is_running = True
    triage_calls: list[bool] = []
    orig_triage_phase = daemon._run_triage_phase

    def _spy_triage_phase(app_session: object, items: list[str], **kw: object):
        triage_calls.append(bool(kw.get("fast_path_only", False)))
        return orig_triage_phase(app_session, items, **kw)

    monkeypatch.setattr(daemon, "_run_triage_phase", _spy_triage_phase)
    try:
        await daemon._triage_file_path(str(inv))
        assert triage_calls == [False]
        assert (ws / "Invoices" / "invoice_fast.txt").is_file()
    finally:
        daemon.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_six_tier_routing_priority_parity_across_daemon_and_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule-matched files with KEYWORD_RULES and a redact policy route identically across daemon and CLI."""
    settings = _make_settings(tmp_path, monkeypatch)
    settings.MAX_WORKERS = 4
    settings.KEYWORD_RULES = {
        "invoice": "Invoices",
        "contract": "Contracts",
        "memo": "Memos",
    }
    settings.POLICIES = [
        {
            "type": "keyword",
            "expression": "redact_me",
            "action": "redact",
            "target_path": "Redacted",
            "priority": 100,
        }
    ]
    settings._save()

    def _populate_workspace(folder: Path) -> list[Path]:
        folder.mkdir(parents=True, exist_ok=True)
        files: list[Path] = []
        for idx in range(12):
            if idx % 4 == 0:
                p = folder / f"invoice_{idx}.txt"
                p.write_text(f"Invoice #{idx} amount $500", encoding="utf-8")
            elif idx % 4 == 1:
                p = folder / f"contract_{idx}.txt"
                p.write_text(f"Contract #{idx} agreement terms", encoding="utf-8")
            elif idx % 4 == 2:
                p = folder / f"redact_me_{idx}.txt"
                p.write_text(f"Record SSN 123-45-6789 #{idx}", encoding="utf-8")
            else:
                p = folder / f"memo_{idx}.txt"
                p.write_text(f"Team memo #{idx}", encoding="utf-8")
            files.append(p)
        return files

    def _collect_sorted_rel_paths(folder: Path) -> set[str]:
        return {
            p.relative_to(folder).as_posix()
            for p in folder.rglob("*.txt")
            if not any(
                part.startswith((".", "_Quarantine"))
                for part in p.relative_to(folder).parts
            )
        }

    expected_paths = (
        {f"Invoices/invoice_{i}.txt" for i in (0, 4, 8)}
        | {f"Contracts/contract_{i}.txt" for i in (1, 5, 9)}
        | {f"Redacted/redact_me_{i}.txt" for i in (2, 6, 10)}
        | {f"Memos/memo_{i}.txt" for i in (3, 7, 11)}
    )

    # 1. CLI SortPipeline run
    cli_ws = tmp_path / "cli_ws"
    _populate_workspace(cli_ws)
    cli_session = AppSession(settings, base_dir=str(cli_ws))
    try:
        _plan, outcome = SortPipeline(cli_session).run(None)
        assert outcome is not None
        assert _collect_sorted_rel_paths(cli_ws) == expected_paths
    finally:
        cli_session.close()

    # 2. Live 4-worker ContinuousWatchdogDaemon burst run
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    daemon_ws = tmp_path / "daemon_ws"
    created = _populate_workspace(daemon_ws)
    daemon = ContinuousWatchdogDaemon(settings, str(daemon_ws))
    try:
        daemon.start()
        for p in created:
            daemon.enqueue_event(
                FileChangeEvent(
                    event_type="created", file_path=str(p), timestamp=time.time()
                )
            )
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline:
            if (
                not [p for p in created if p.exists()]
                and _collect_sorted_rel_paths(daemon_ws) == expected_paths
            ):
                break
            time.sleep(0.05)

        assert _collect_sorted_rel_paths(daemon_ws) == expected_paths
        bad_logs = [
            rec.getMessage()
            for rec in caplog.records
            if "Failed to stage incoming file" in rec.getMessage()
            or "unwind_session" in rec.getMessage()
            or "Rolling back session" in rec.getMessage()
            or "IllegalStateTransitionError" in rec.getMessage()
            or (
                rec.exc_info
                and rec.exc_info[0] is not None
                and issubclass(rec.exc_info[0], IllegalStateTransitionError)
            )
        ]
        assert bad_logs == []
    finally:
        daemon.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_history_beats_jev_category_while_sensitivity_quarantine_beats_keywords(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 3 sensitivity quarantine beats KEYWORD_RULES, and Tier 6 history beats JEV category."""
    settings = _make_settings(tmp_path, monkeypatch)
    settings.AUTO_QUARANTINE_RATINGS = ["RESTRICTED"]
    settings.KEYWORD_RULES = {"invoice": "Invoices"}
    settings._save()

    ws = tmp_path / "prio_ws"
    ws.mkdir()
    restricted_inv = ws / "invoice_secret.txt"
    restricted_inv.write_text("Restricted invoice", encoding="utf-8")
    hist_doc = ws / "report_q4.txt"
    hist_bytes = b"Historical quarterly report"
    hist_doc.write_bytes(hist_bytes)
    hist_hash = hashlib.sha256(hist_bytes).hexdigest()

    session = AppSession(settings, base_dir=str(ws))
    try:
        session.db.upsert_documents(
            [
                (str(ws), "invoice_secret.txt", "h1", "Restricted invoice"),
                (str(ws), "report_q4.txt", hist_hash, "Historical quarterly report"),
            ]
        )
        session.db.set_user_verified_target(str(ws), hist_hash, "CustomHistory")

        jev_results = {
            "invoice_secret.txt": JevClassificationResult(
                category="Financial Reports",
                sensitivity_rating="RESTRICTED",
                sensitivity_score=0.95,
                is_classified=True,
                confidence=0.95,
            ),
            "report_q4.txt": JevClassificationResult(
                category="Financial Reports",
                sensitivity_rating="LOW",
                sensitivity_score=0.1,
                is_classified=True,
                confidence=0.9,
            ),
        }
        plan = session.analyzer.generate_sorting_plan(
            str(ws),
            settings,
            jev_results=jev_results,
        )
        assert "_Compliance_Quarantine" in plan
        assert "invoice_secret.txt" in plan["_Compliance_Quarantine"]
        assert "CustomHistory" in plan
        assert "report_q4.txt" in plan["CustomHistory"]
        assert "Financial Reports" not in plan
    finally:
        session.close()
        clear_connection_cache(only_current_and_inactive=False)


def test_worker_crash_sets_health_degraded_and_stop_vs_monitor_raises_zero_value_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker crash marks health telemetry degraded with last_error, and concurrent stop() vs monitor raises 0 ValueErrors."""
    settings = _make_settings(tmp_path, monkeypatch)
    ws = tmp_path / "health_ws"
    ws.mkdir()

    daemon = ContinuousWatchdogDaemon(settings, str(ws))
    try:
        daemon.start()
        assert daemon.get_health_telemetry()["status"] == "ok"
        assert daemon.get_health_telemetry()["last_error"] is None

        # Simulate an unhandled exception in _process_single_event on a worker
        sample = ws / "crash_trigger.txt"
        with mock.patch.object(
            daemon,
            "_process_single_event",
            side_effect=RuntimeError("simulated worker boom"),
        ):
            sample.write_text("trigger", encoding="utf-8")
            daemon.enqueue_event(
                FileChangeEvent(
                    event_type="created", file_path=str(sample), timestamp=time.time()
                )
            )
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                health = daemon.get_health_telemetry()
                if health["status"] == "degraded":
                    break
                time.sleep(0.02)

        health = daemon.get_health_telemetry()
        assert health["status"] == "degraded"
        assert "simulated worker boom" in str(health["last_error"])
    finally:
        daemon.stop()

    # Verify non-zero subprocess exit sets degraded health when not stopped
    class _CrashedProc:
        def __init__(self, exitcode: int = 1) -> None:
            self._closed = False
            self._exitcode = exitcode

        @property
        def exitcode(self) -> int:
            if self._closed:
                raise ValueError("process object is closed")
            return self._exitcode

        def is_alive(self) -> bool:
            if self._closed:
                raise ValueError("process object is closed")
            return False

        def join(self, timeout: float | None = None) -> None:
            _ = timeout
            if self._closed:
                raise ValueError("process object is closed")

        def terminate(self) -> None:
            if self._closed:
                raise ValueError("process object is closed")

        def kill(self) -> None:
            if self._closed:
                raise ValueError("process object is closed")

        def close(self) -> None:
            if self._closed:
                raise ValueError("process object is closed")
            self._closed = True

    daemon2 = ContinuousWatchdogDaemon(settings, str(ws))
    daemon2._is_running = True
    crashed: Any = _CrashedProc(exitcode=1)
    daemon2._sorting_process = crashed
    daemon2._monitor_sorting_process(crashed)
    health2 = daemon2.get_health_telemetry()
    assert health2["status"] == "degraded"
    assert "exited with code 1" in str(health2["last_error"])
    daemon2.stop()

    # Hammer stop() vs _monitor_sorting_process concurrently across 40 iterations
    raised_errors: list[BaseException] = []
    for _ in range(40):
        d = ContinuousWatchdogDaemon(settings, str(ws))
        d._is_running = True
        barrier = threading.Barrier(2)

        class _RacyProc(_CrashedProc):
            def __init__(self) -> None:
                super().__init__(exitcode=0)
                self._alive = True

            def is_alive(self) -> bool:
                if self._closed:
                    raise ValueError("process object is closed")
                return self._alive

            def terminate(self) -> None:
                if self._closed:
                    raise ValueError("process object is closed")
                self._alive = False

            def join(self, timeout: float | None = None) -> None:
                _ = timeout
                if self._closed:
                    raise ValueError("process object is closed")
                self._alive = False

        proc: Any = _RacyProc()
        d._sorting_process = proc

        def _run_monitor(
            inst: ContinuousWatchdogDaemon = d, p: _RacyProc = proc
        ) -> None:
            try:
                barrier.wait(timeout=2.0)
                inst._monitor_sorting_process(p)
            except BaseException as exc:
                raised_errors.append(exc)

        def _run_stop(inst: ContinuousWatchdogDaemon = d) -> None:
            try:
                barrier.wait(timeout=2.0)
                inst.stop()
            except BaseException as exc:
                raised_errors.append(exc)

        t_mon = threading.Thread(target=_run_monitor)
        t_stp = threading.Thread(target=_run_stop)
        t_mon.start()
        t_stp.start()
        t_mon.join(timeout=5.0)
        t_stp.join(timeout=5.0)

    assert raised_errors == []
    clear_connection_cache(only_current_and_inactive=False)


@pytest.mark.anyio
async def test_ipc_drop_with_dest_dir_validates_runs_on_sort_executor_in_scoped_move_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON-RPC drop with dest_dir validates dest_dir, runs on _sort_executor inside scoped_move_phase, and moves into dest_dir."""
    settings = _make_settings(tmp_path, monkeypatch)
    settings.KEYWORD_RULES = {"invoice": "Invoices", "contract": "Contracts"}
    protected_dest = tmp_path / "protected_dest"
    protected_dest.mkdir()
    settings.PROTECTED_PATHS = [str(protected_dest)]
    settings._save()

    watched = tmp_path / "watched_ipc"
    watched.mkdir()
    incoming = tmp_path / "incoming_drop"
    incoming.mkdir()
    dest_dir = tmp_path / "custom_dest"
    dest_dir.mkdir()
    not_a_dir = tmp_path / "file_not_dir.txt"
    not_a_dir.write_text("not a directory", encoding="utf-8")

    f_inv = incoming / "invoice_drop.txt"
    f_inv.write_text("Invoice #99 for $1200", encoding="utf-8")
    f_con = incoming / "contract_drop.txt"
    f_con.write_text("Signed contract agreement", encoding="utf-8")

    daemon = ContinuousWatchdogDaemon(settings, str(watched))
    daemon._is_running = True
    server = DaemonIPCServer(daemon)
    await server.start()
    client = DaemonIPCClient(str(watched))

    observed_thread_names: list[str] = []
    observed_is_moving: list[bool] = []
    orig_exec_batch = None

    from app.core.pipeline import DropBatchCoordinator, DropBatchPlan

    orig_exec_batch = DropBatchCoordinator.execute_batch

    def _spy_exec_batch(
        self_coord: DropBatchCoordinator, batch_plan: DropBatchPlan, **kw: Any
    ):
        observed_thread_names.append(threading.current_thread().name)
        observed_is_moving.append(daemon.is_moving)
        return orig_exec_batch(self_coord, batch_plan, **kw)

    monkeypatch.setattr(DropBatchCoordinator, "execute_batch", _spy_exec_batch)

    try:
        # 1. Invalid dest_dir (file instead of directory) is rejected
        with pytest.raises(RuntimeError, match="not a directory"):
            await client.send_command(
                "drop",
                {"paths": [str(f_inv)], "dest_dir": str(not_a_dir)},
            )

        # 2. Protected dest_dir is rejected
        with pytest.raises(RuntimeError, match="protected"):
            await client.send_command(
                "drop",
                {"paths": [str(f_inv)], "dest_dir": str(protected_dest)},
            )

        # 3. Valid dest_dir runs on _sort_executor inside scoped_move_phase and moves into dest_dir
        res = await client.send_command(
            "drop",
            {
                "paths": [str(f_inv), str(f_con)],
                "dest_dir": str(dest_dir),
                "dry_run": False,
            },
        )
        assert res["status"] == "success"
        assert res["destination_dir"] == str(dest_dir.resolve())
        assert res["moved_count"] == 2
        assert observed_thread_names and all(
            t.startswith("daemon-sort") for t in observed_thread_names
        )
        assert observed_is_moving == [True]
        assert not f_inv.exists()
        assert not f_con.exists()
        assert (dest_dir / "Invoices" / "invoice_drop.txt").is_file()
        assert (dest_dir / "Contracts" / "contract_drop.txt").is_file()
    finally:
        server.stop()
        daemon.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_phase0_coalesced_staging_caches_released_doc_and_persists_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_stage_and_process_phase0 caches released text in one write and persists full audit logs on holds."""
    settings = _make_settings(tmp_path, monkeypatch)
    ws = tmp_path / "coalesce_ws"
    ws.mkdir()

    clean_doc = ws / "clean_note.txt"
    clean_doc.write_text("Quarterly summary note", encoding="utf-8")
    held_doc = ws / "secret_hold.txt"
    held_doc.write_text("Top secret quarantine_token payload", encoding="utf-8")

    session = AppSession(settings, base_dir=str(ws))
    try:
        interceptor = QuarantineInterceptorService(
            db=session.db,
            policies=[
                {
                    "type": "keyword",
                    "expression": "quarantine_token",
                    "action": "quarantine",
                    "target_path": "_Compliance_Quarantine",
                    "priority": 100,
                }
            ],
            runtime_settings=settings,
        )

        staged_rel, rel_res = interceptor._stage_and_process_phase0(
            source_path=str(clean_doc),
            base_dir=str(ws),
            original_relative_path="clean_note.txt",
            cache_released_document=True,
        )
        assert rel_res.status == "RELEASED"
        assert not os.path.exists(staged_rel["staged_filepath"])
        assert session.db.get_quarantine_record(staged_rel["job_id"]) is None
        cached = session.db.get_document(str(ws), "clean_note.txt")
        assert cached is not None
        assert cached["file_hash"] == staged_rel["file_hash"]
        assert "Quarterly summary note" in cached["extracted_text"]

        staged_hold, hold_res = interceptor._stage_and_process_phase0(
            source_path=str(held_doc),
            base_dir=str(ws),
            original_relative_path="secret_hold.txt",
            cache_released_document=True,
        )
        assert hold_res.status == "QUARANTINED"
        persisted = session.db.get_quarantine_record(staged_hold["job_id"])
        assert persisted is not None
        assert persisted["status"] == "QUARANTINED"
        audit_statuses = [entry["status"] for entry in persisted["audit_log"]]
        assert audit_statuses == ["STAGED", "IN_INSPECTION", "QUARANTINED"]
    finally:
        session.close()
        clear_connection_cache(only_current_and_inactive=False)


def test_mid_triage_external_event_schedules_followup_retriage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Events arriving while a path is in _active_triage_paths schedule a follow-up triage if unmoved."""
    settings = _make_settings(tmp_path, monkeypatch)
    settings.DEDUP_WINDOW = 5.0
    settings.POLICIES = [
        {
            "type": "keyword",
            "expression": "late_invoice_marker",
            "action": "retain",
            "target_path": "Invoices",
            "priority": 100,
        }
    ]
    settings._save()

    ws = tmp_path / "retriage_ws"
    ws.mkdir()
    target_file = ws / "draft.txt"
    target_file.write_text("initial placeholder", encoding="utf-8")

    daemon = ContinuousWatchdogDaemon(settings, str(ws))
    first_entered = threading.Event()
    allow_finish_first = threading.Event()
    triage_count = 0
    orig_triage = daemon._triage_file_path

    async def _wrapped_triage(
        abs_path: str, cancel_token: threading.Event | None = None
    ) -> None:
        nonlocal triage_count
        triage_count += 1
        if triage_count == 1:
            first_entered.set()
            await __import__("asyncio").to_thread(allow_finish_first.wait, 3.0)
            # First triage sees placeholder text via pre-rewrite snapshot or skips; simulate
            # mid-triage external rewrite arriving while _active_triage_paths holds norm_p.
            return
        await orig_triage(abs_path, cancel_token=cancel_token)

    monkeypatch.setattr(daemon, "_triage_file_path", _wrapped_triage)

    try:
        daemon.start()
        daemon.enqueue_event(FileChangeEvent("created", str(target_file)))
        assert first_entered.wait(timeout=3.0)

        # Rewrite file with matching content while first triage is active and inside DEDUP_WINDOW
        target_file.write_text("Updated late_invoice_marker #777", encoding="utf-8")
        assert (
            daemon.enqueue_event(FileChangeEvent("modified", str(target_file))) is False
        )

        allow_finish_first.set()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if (ws / "Invoices" / "draft.txt").is_file():
                break
            time.sleep(0.05)

        assert triage_count >= 2
        assert not target_file.exists()
        assert (ws / "Invoices" / "draft.txt").is_file()
    finally:
        allow_finish_first.set()
        daemon.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_phase0_cached_document_bypasses_duplicate_phase2_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Phase 0 caches supported text under content policies, Phase 2 passes bypassed_items and skips build_corpus_generator."""
    import app.core.extractor as extractor_mod

    settings = _make_settings(tmp_path, monkeypatch)
    settings.DEDUP_WINDOW = 0.01
    settings.KEYWORD_RULES = {"invoice": "Invoices"}
    settings.POLICIES = [
        {
            "type": "keyword",
            "expression": "redact_secret_marker",
            "action": "redact",
            "target_path": "Redacted",
            "priority": 100,
        }
    ]
    settings._save()

    ws = tmp_path / "bypass_ws"
    ws.mkdir()

    corpus_calls: list[list[str]] = []
    orig_build_corpus = extractor_mod.build_corpus_generator

    def _spy_build_corpus(*args: Any, **kwargs: Any) -> Any:
        items = list(kwargs.get("items_to_sort") or (args[1] if len(args) > 1 else []))
        corpus_calls.append([str(i) for i in items])
        return orig_build_corpus(*args, **kwargs)

    monkeypatch.setattr(extractor_mod, "build_corpus_generator", _spy_build_corpus)

    daemon = ContinuousWatchdogDaemon(settings, str(ws))
    try:
        daemon.start()
        inv = ws / "invoice_99.txt"
        inv.write_text("Invoice #99 standard billing summary", encoding="utf-8")
        red = ws / "secret_99.txt"
        red.write_text(
            "Confidential redact_secret_marker SSN 123-45-6789", encoding="utf-8"
        )
        daemon.enqueue_event(FileChangeEvent("created", str(inv)))
        daemon.enqueue_event(FileChangeEvent("created", str(red)))

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if (ws / "Invoices" / "invoice_99.txt").is_file() and (
                ws / "Redacted" / "secret_99.txt"
            ).is_file():
                break
            time.sleep(0.05)

        assert (ws / "Invoices" / "invoice_99.txt").is_file()
        assert (ws / "Redacted" / "secret_99.txt").is_file()
        scrubbed = (ws / "Redacted" / "secret_99.txt").read_text(encoding="utf-8")
        assert "123-45-6789" not in scrubbed
        assert corpus_calls == []
    finally:
        daemon.stop()
        clear_connection_cache(only_current_and_inactive=False)
