"""Tests for targeted single-path event triage and target-filtered plan generation."""

import asyncio
import logging
from unittest import mock

import pytest
from watchdog.events import FileCreatedEvent

from app.core.analyzer import IncrementalAnalyzer
from app.core.daemon import (
    ContinuousWatchdogDaemon,
    DaemonFolderHandler,
    FileChangeEvent,
)
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.session import AppSession


class DummySettings:
    """Mock configuration for testing."""

    def __init__(self):
        self.DEBOUNCE_DELAY = 0.6
        self.MAX_DEBOUNCE_DELAY = 5.0
        self.IGNORED_EXTENSIONS = [".crdownload", ".tmp", ".download"]
        self.LOG_FILE = "test.log"
        self.CONFLICT_POLICY = "rename"
        self.MAX_FOLDERS = 10
        self.STOP_WORDS = set()
        self.KEYWORD_RULES = {"invoice": "Invoices"}
        self.LEARNED_RULES = {}
        self.POLICIES = []
        self.AI_CONSENT_GRANTED = True

    def load(self):
        self.loaded_ok = True


def test_daemon_handler_enqueues_event_without_recalculation(tmp_path):
    """Verify DaemonFolderHandler.on_any_event enqueues FileChangeEvent and does not trigger legacy recalculation."""
    from app.core.db_conn import clear_connection_cache

    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True
    daemon._event_queue = asyncio.PriorityQueue()

    try:
        handler = DaemonFolderHandler(daemon)
        test_file = tmp_path / "incoming_invoice.pdf"
        test_file.write_text("invoice content")

        event = FileCreatedEvent(str(test_file))
        handler.on_any_event(event)

        # Verify event was enqueued
        assert daemon._event_queue.qsize() == 1
        raw_item = daemon._event_queue.get_nowait()
        enqueued = raw_item[2] if isinstance(raw_item, tuple) else raw_item
        assert isinstance(enqueued, FileChangeEvent)
        assert enqueued.file_path == str(test_file)
        assert enqueued.event_type == "created"

        # Verify legacy global recalculation timer was NOT started
        assert daemon._debounce_timer is None
    finally:
        daemon.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_analyzer_and_session_generate_sorting_plan_target_paths_filter(tmp_path):
    """Verify generate_sorting_plan accepts target_paths and restricts plan output to target file paths."""
    from app.core.db_conn import clear_connection_cache

    settings = DummySettings()
    db_path = tmp_path / "autosorter.db"
    worker = DBWorker()
    db = Database(db_path, worker)

    try:
        base_dir = str(tmp_path)
        db.upsert_documents(
            [
                (base_dir, "doc1_invoice.pdf", "hash1", "invoice payment details"),
                (base_dir, "doc2_report.pdf", "hash2", "quarterly financial report"),
                (base_dir, "doc3_notes.txt", "hash3", "meeting notes"),
            ]
        )

        analyzer = IncrementalAnalyzer(
            stop_words=set(), max_folders=10, strategy_name="default", db=db
        )

        # Test Analyzer.generate_sorting_plan with target_paths restricting to doc1_invoice.pdf
        plan_doc1 = analyzer.generate_sorting_plan(
            base_dir,
            runtime_settings=settings,
            target_paths=["doc1_invoice.pdf"],
        )

        # Flatten plan keys
        keys_doc1 = list(plan_doc1.keys()) if isinstance(plan_doc1, dict) else []
        assert len(keys_doc1) == 1
        assert (
            "Invoices" in keys_doc1
            or "doc1_invoice.pdf" in keys_doc1
            or "doc1_invoice.pdf" in str(plan_doc1)
        )

        # Test AppSession.generate_sorting_plan with target_paths restricting to doc2_report.pdf
        session = AppSession(settings, base_dir)
        session.db = db
        session.analyzer = analyzer

        plan_doc2 = session.generate_sorting_plan(
            fast_path_only=False,
            target_paths=["doc2_report.pdf"],
        )

        # Convert plan to string or dict check
        plan_str = str(plan_doc2)
        assert "doc2_report.pdf" in plan_str
        assert "doc1_invoice.pdf" not in plan_str
        assert "doc3_notes.txt" not in plan_str

        session.close()
    finally:
        worker.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_db_get_all_documents_targeted_query(tmp_path):
    """Verify get_all_documents with target_paths executes targeted query without loading all docs when cache empty."""
    from app.core.db_conn import clear_connection_cache

    db_path = tmp_path / "autosorter.db"
    worker = DBWorker()
    db = Database(db_path, worker)

    try:
        base_dir = str(tmp_path)
        db.upsert_documents(
            [
                (base_dir, "fileA.txt", "hashA", "Content A"),
                (base_dir, "fileB.txt", "hashB", "Content B"),
                (base_dir, "fileC.txt", "hashC", "Content C"),
            ]
        )

        # Ensure cache is invalidated
        db.invalidate_cache()

        # Query specifically for fileB.txt
        docs = db.get_all_documents(base_dir, target_paths=["fileB.txt"])

        assert len(docs) == 1
        assert docs[0][0] == "fileB.txt"
        assert docs[0][1] == "Content B"
    finally:
        worker.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_db_get_all_documents_zero_decryption_on_unrequested_rows(tmp_path, mocker):
    """Verify worker threads decrypt only rows confirmed by the pre-decryption guard and execute zero calls on unrequested rows."""
    from app.core.db_conn import clear_connection_cache

    db_path = tmp_path / "autosorter.db"
    worker = DBWorker()
    db = Database(db_path, worker)

    try:
        base_dir = str(tmp_path)
        db.upsert_documents(
            [
                (base_dir, "file1.txt", "hash1", "Content 1"),
                (base_dir, "file2.txt", "hash2", "Content 2"),
                (base_dir, "file3.txt", "hash3", "Content 3"),
                (base_dir, "file4.txt", "hash4", "Content 4"),
                (base_dir, "file5.txt", "hash5", "Content 5"),
            ]
        )
        db.invalidate_cache()

        decrypt_spy = mocker.spy(db.crypto, "decrypt_text")

        # Query specifically for file2.txt
        docs = db.get_all_documents(base_dir, target_paths=["file2.txt"])

        assert len(docs) == 1
        assert docs[0][0] == "file2.txt"
        assert docs[0][1] == "Content 2"

        # Exactly 1 decryption call occurred on worker threads for the requested file, 0 on unrequested rows
        assert decrypt_spy.call_count == 1
    finally:
        worker.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_db_get_all_documents_chunked_queries_500_items(tmp_path, mocker):
    """Verify targeted lookups utilize parameterized filepath clauses chunked at 500 items."""
    import re

    from app.core.db_conn import clear_connection_cache, get_db_connection

    db_path = tmp_path / "autosorter.db"
    worker = DBWorker()
    db = Database(db_path, worker)

    try:
        base_dir = str(tmp_path)
        # Create 1200 target paths to force chunking across multiple 500-item batches
        target_paths = [f"batch_doc_{i:04d}.txt" for i in range(1200)]

        # Insert 3 records corresponding to items in chunk 0, chunk 1, and chunk 2
        db.upsert_documents(
            [
                (base_dir, target_paths[10], "hash10", "Batch Content 10"),
                (base_dir, target_paths[550], "hash550", "Batch Content 550"),
                (base_dir, target_paths[1150], "hash1150", "Batch Content 1150"),
            ]
        )
        db.invalidate_cache()

        # Intercept connection execute calls to record SQL queries and parameters
        real_conn = get_db_connection(str(db_path))
        captured_queries = []

        class ConnProxy:
            def __init__(self, raw):
                self._raw = raw

            def __enter__(self):
                self._raw.__enter__()
                return self

            def __exit__(self, *args):
                return self._raw.__exit__(*args)

            def execute(self, sql, *args):
                if "FROM documents" in sql and "filepath IN" in sql:
                    params = args[0] if args else ()
                    captured_queries.append((sql, params))
                return self._raw.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._raw, name)

        proxy = ConnProxy(real_conn)
        mocker.patch("app.core.db.get_db_connection", return_value=proxy)

        docs = db.get_all_documents(base_dir, target_paths=target_paths)

        # 1200 items chunked at 500 items yields ceil(1200 / 500) = 3 query batches
        assert len(captured_queries) == 3

        # Validate chunk sizing
        for idx, (sql, params) in enumerate(captured_queries):
            # Find the filepath IN clause placeholders
            match = re.search(r"filepath IN \(([^)]+)\)", sql)
            assert match is not None
            placeholders = match.group(1).split(",")
            if idx in (0, 1):
                assert len(placeholders) == 500
            else:
                assert len(placeholders) == 200

        assert len(docs) == 3
        returned_fps = {d[0] for d in docs}
        assert returned_fps == {target_paths[10], target_paths[550], target_paths[1150]}
    finally:
        worker.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_db_get_all_documents_multi_root_targeted_guard(tmp_path, mocker):
    """Verify multi-root targeted queries only decrypt rows confirmed for the specific root key."""
    from app.core.db_conn import clear_connection_cache

    root1 = str(tmp_path / "root1")
    root2 = str(tmp_path / "root2")
    db_path = tmp_path / "autosorter.db"
    worker = DBWorker()
    db = Database(db_path, worker)

    try:
        # Both roots have a file with the identical relative filepath 'shared_report.pdf'
        db.upsert_documents(
            [
                (root1, "shared_report.pdf", "hash1", "Root 1 Report"),
                (root2, "shared_report.pdf", "hash2", "Root 2 Report"),
            ]
        )
        db.invalidate_cache()

        decrypt_spy = mocker.spy(db.crypto, "decrypt_text")

        # Query targeting only root1's version
        docs = db.get_all_documents(
            root1,
            target_paths=[f"{root1}::shared_report.pdf"],
            source_dirs=[root1, root2],
        )

        assert len(docs) == 1
        assert docs[0][0] == f"{root1}::shared_report.pdf"
        assert docs[0][1] == "Root 1 Report"

        # Worker threads decrypted ONLY root1's row; root2's row was blocked by the pre-decryption guard
        assert decrypt_spy.call_count == 1
    finally:
        worker.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_db_get_all_documents_empty_target_paths(tmp_path, mocker):
    """Verify empty target_paths immediately returns empty list without database or decryption overhead."""
    import app.core.db
    from app.core.db_conn import clear_connection_cache

    db_path = tmp_path / "autosorter.db"
    worker = DBWorker()
    db = Database(db_path, worker)

    try:
        base_dir = str(tmp_path)
        db.upsert_documents([(base_dir, "doc.txt", "hash", "Some Content")])
        db.invalidate_cache()

        decrypt_spy = mocker.spy(db.crypto, "decrypt_text")
        conn_spy = mocker.spy(app.core.db, "get_db_connection")

        docs = db.get_all_documents(base_dir, target_paths=[])
        assert docs == []
        assert decrypt_spy.call_count == 0
        assert conn_spy.call_count == 0
    finally:
        worker.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_db_get_all_documents_indexed_primary_key_lookup(tmp_path):
    """Verify that chunked targeted queries leverage the composite primary key index without full table scans."""
    from app.core.db_conn import clear_connection_cache, get_db_connection

    db_path = tmp_path / "autosorter.db"
    worker = DBWorker()
    db = Database(db_path, worker)

    try:
        base_dir = str(tmp_path)
        db.upsert_documents([(base_dir, "doc1.txt", "hash1", "Content 1")])
        db.invalidate_cache()

        conn = get_db_connection(str(db_path))
        query = (
            "EXPLAIN QUERY PLAN "
            "SELECT base_dir, filepath, extracted_text, file_hash, user_verified_target_path "
            "FROM documents WHERE base_dir IN (?) AND filepath IN (?)"
        )
        plan_rows = conn.execute(query, (base_dir, "doc1.txt")).fetchall()
        plan_text = " ".join(str(r) for r in plan_rows)

        # Confirm SQLite uses the primary key index (sqlite_autoindex_documents_1)
        assert "sqlite_autoindex_documents_1" in plan_text
        assert "SCAN" not in plan_text
        assert "SEARCH" in plan_text
    finally:
        worker.stop()
        clear_connection_cache(only_current_and_inactive=False)


@pytest.mark.anyio
async def test_triage_file_path_passes_target_paths_and_no_get_files_recursively(
    tmp_path,
    session_db,
):
    """Verify ContinuousWatchdogDaemon._triage_file_path passes target_paths to generate_sorting_plan without calling get_files_recursively."""
    from app.core.db_conn import clear_connection_cache

    settings = DummySettings()
    settings.AUTO_QUARANTINE_RATINGS = []
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    test_file = tmp_path / "single_drop_invoice.pdf"
    test_file.write_text("invoice text")

    mock_app_session = mock.MagicMock()
    mock_app_session.db = session_db
    mock_app_session.generate_sorting_plan.return_value = {
        "Invoices": {"single_drop_invoice.pdf": {"__type__": "file"}}
    }
    mock_app_session.execute_moves.return_value = {"moved": 1}

    try:
        with (
            mock.patch.object(
                daemon, "_get_or_create_session", return_value=mock_app_session
            ),
            mock.patch("app.core.daemon.get_files_recursively") as mock_get_files,
            mock.patch("app.core.metadata.MetadataPass.run"),
        ):
            await daemon._triage_file_path(str(test_file))

            # Ensure get_files_recursively was NOT called during single file triage
            mock_get_files.assert_not_called()

            # Ensure generate_sorting_plan was called with target_paths=["single_drop_invoice.pdf"]
            assert mock_app_session.generate_sorting_plan.called
            call_kwargs = mock_app_session.generate_sorting_plan.call_args.kwargs
            assert call_kwargs.get("target_paths") == ["single_drop_invoice.pdf"]
    finally:
        daemon.stop()
        session_db.worker.flush()
        clear_connection_cache(only_current_and_inactive=False)


@pytest.mark.anyio
async def test_triage_file_path_missing_file_handled_gracefully(tmp_path, caplog):
    """Verify that if a target file is missing before triage completes, the task logs a debug message and completes gracefully."""
    from app.core.db_conn import clear_connection_cache

    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    missing_path = str(tmp_path / "non_existent_file.pdf")

    try:
        with caplog.at_level(logging.DEBUG):
            await daemon._triage_file_path(missing_path)

        assert "Target file missing before triage" in caplog.text
    finally:
        daemon.stop()
        clear_connection_cache(only_current_and_inactive=False)


@pytest.mark.anyio
async def test_reconciliation_audit_enqueues_untracked_files(tmp_path):
    """Verify periodic reconciliation audit scans directory and enqueues untracked files into the queue."""
    from app.core.db_conn import clear_connection_cache

    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True
    daemon._event_loop = None
    daemon._event_queue = asyncio.PriorityQueue()

    untracked1 = tmp_path / "untracked1.pdf"
    untracked1.write_text("untracked file 1")

    try:
        with mock.patch(
            "app.core.daemon.get_files_recursively", return_value=[str(untracked1)]
        ):
            await daemon._run_reconciliation_audit()

        assert daemon._event_queue.qsize() == 1
        raw_item = daemon._event_queue.get_nowait()
        enqueued = raw_item[2] if isinstance(raw_item, tuple) else raw_item
        assert enqueued.event_type == "reconciliation"
        assert enqueued.file_path == str(untracked1)
    finally:
        daemon.stop()
        clear_connection_cache(only_current_and_inactive=False)


@pytest.mark.anyio
async def test_triage_moves_files_off_the_event_loop(tmp_path, session_db):
    """Blocking sort work runs on the sort thread, so the loop stays responsive (#717)."""
    import threading

    from app.core.db_conn import clear_connection_cache

    settings = DummySettings()
    settings.AUTO_QUARANTINE_RATINGS = []
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    test_file = tmp_path / "slow_invoice.pdf"
    test_file.write_text("invoice text")

    release = threading.Event()
    move_threads = []

    def slow_move(plan):
        move_threads.append(threading.current_thread().name)
        assert release.wait(10)
        return {"moved": 1}

    mock_app_session = mock.MagicMock()
    mock_app_session.db = session_db
    mock_app_session.generate_sorting_plan.return_value = {
        "Invoices": {"slow_invoice.pdf": {"__type__": "file"}}
    }
    mock_app_session.execute_moves.side_effect = slow_move

    try:
        with (
            mock.patch.object(
                daemon, "_get_or_create_session", return_value=mock_app_session
            ),
            mock.patch("app.core.metadata.MetadataPass.run"),
        ):
            triage = asyncio.create_task(daemon._triage_file_path(str(test_file)))
            for _ in range(200):
                if move_threads:
                    break
                await asyncio.sleep(0.01)
            # The move is blocked, yet this coroutine keeps running.
            assert move_threads and not triage.done()
            release.set()
            await asyncio.wait_for(triage, 10)

        assert move_threads[0].startswith("daemon-sort")
        assert threading.current_thread().name not in move_threads
    finally:
        release.set()
        daemon.stop()
        session_db.worker.flush()
        clear_connection_cache(only_current_and_inactive=False)
