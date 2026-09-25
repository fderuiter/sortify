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
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True
    daemon._event_queue = asyncio.Queue()

    handler = DaemonFolderHandler(daemon)
    test_file = tmp_path / "incoming_invoice.pdf"
    test_file.write_text("invoice content")

    event = FileCreatedEvent(str(test_file))
    handler.on_any_event(event)

    # Verify event was enqueued
    assert daemon._event_queue.qsize() == 1
    enqueued = daemon._event_queue.get_nowait()
    assert isinstance(enqueued, FileChangeEvent)
    assert enqueued.file_path == str(test_file)
    assert enqueued.event_type == "created"

    # Verify legacy global recalculation timer was NOT started
    assert daemon._debounce_timer is None

    daemon.stop()


def test_analyzer_and_session_generate_sorting_plan_target_paths_filter(tmp_path):
    """Verify generate_sorting_plan accepts target_paths and restricts plan output to target file paths."""
    settings = DummySettings()
    db_path = tmp_path / "autosorter.db"
    worker = DBWorker()
    db = Database(db_path, worker)

    base_dir = str(tmp_path)
    db.upsert_documents([
        (base_dir, "doc1_invoice.pdf", "hash1", "invoice payment details"),
        (base_dir, "doc2_report.pdf", "hash2", "quarterly financial report"),
        (base_dir, "doc3_notes.txt", "hash3", "meeting notes"),
    ])

    analyzer = IncrementalAnalyzer(stop_words=set(), max_folders=10, strategy_name="default", db=db)

    # Test Analyzer.generate_sorting_plan with target_paths restricting to doc1_invoice.pdf
    plan_doc1 = analyzer.generate_sorting_plan(
        base_dir,
        runtime_settings=settings,
        target_paths=["doc1_invoice.pdf"],
    )

    # Flatten plan keys
    keys_doc1 = list(plan_doc1.keys()) if isinstance(plan_doc1, dict) else []
    assert len(keys_doc1) == 1
    assert "Invoices" in keys_doc1 or "doc1_invoice.pdf" in keys_doc1 or "doc1_invoice.pdf" in str(plan_doc1)

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


def test_db_get_all_documents_targeted_query(tmp_path):
    """Verify get_all_documents with target_paths executes targeted query without loading all docs when cache empty."""
    db_path = tmp_path / "autosorter.db"
    worker = DBWorker()
    db = Database(db_path, worker)

    base_dir = str(tmp_path)
    db.upsert_documents([
        (base_dir, "fileA.txt", "hashA", "Content A"),
        (base_dir, "fileB.txt", "hashB", "Content B"),
        (base_dir, "fileC.txt", "hashC", "Content C"),
    ])

    # Ensure cache is invalidated
    db.invalidate_cache()

    # Query specifically for fileB.txt
    docs = db.get_all_documents(base_dir, target_paths=["fileB.txt"])

    assert len(docs) == 1
    assert docs[0][0] == "fileB.txt"
    assert docs[0][1] == "Content B"


@pytest.mark.anyio
async def test_triage_file_path_passes_target_paths_and_no_get_files_recursively(tmp_path):
    """Verify ContinuousWatchdogDaemon._triage_file_path passes target_paths to generate_sorting_plan without calling get_files_recursively."""
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    test_file = tmp_path / "single_drop_invoice.pdf"
    test_file.write_text("invoice text")

    mock_app_session = mock.MagicMock()
    mock_app_session.generate_sorting_plan.return_value = {"Invoices": {"single_drop_invoice.pdf": {"__type__": "file"}}}
    mock_app_session.execute_moves.return_value = {"moved": 1}

    with mock.patch.object(daemon, "_get_or_create_session", return_value=mock_app_session), \
         mock.patch("app.core.daemon.get_files_recursively") as mock_get_files, \
         mock.patch("app.core.daemon.MetadataPass.run"):

        await daemon._triage_file_path(str(test_file))

        # Ensure get_files_recursively was NOT called during single file triage
        mock_get_files.assert_not_called()

        # Ensure generate_sorting_plan was called with target_paths=["single_drop_invoice.pdf"]
        assert mock_app_session.generate_sorting_plan.called
        call_kwargs = mock_app_session.generate_sorting_plan.call_args.kwargs
        assert call_kwargs.get("target_paths") == ["single_drop_invoice.pdf"]

    daemon.stop()


@pytest.mark.anyio
async def test_triage_file_path_missing_file_handled_gracefully(tmp_path, caplog):
    """Verify that if a target file is missing before triage completes, the task logs a debug message and completes gracefully."""
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    missing_path = str(tmp_path / "non_existent_file.pdf")

    with caplog.at_level(logging.DEBUG):
        await daemon._triage_file_path(missing_path)

    assert "Target file missing before triage" in caplog.text
    daemon.stop()


@pytest.mark.anyio
async def test_reconciliation_audit_enqueues_untracked_files(tmp_path):
    """Verify periodic reconciliation audit scans directory and enqueues untracked files into the queue."""
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True
    daemon._event_loop = None
    daemon._event_queue = asyncio.Queue()

    untracked1 = tmp_path / "untracked1.pdf"
    untracked1.write_text("untracked file 1")

    with mock.patch("app.core.daemon.get_files_recursively", return_value=[str(untracked1)]):
        await daemon._run_reconciliation_audit()

    assert daemon._event_queue.qsize() == 1
    enqueued = daemon._event_queue.get_nowait()
    assert enqueued.event_type == "reconciliation"
    assert enqueued.file_path == str(untracked1)

    daemon.stop()
