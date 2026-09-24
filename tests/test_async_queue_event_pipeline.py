"""Unit and integration tests for the queue-based in-memory event pipeline with async triage workers."""

import asyncio
import os
import time
from unittest import mock

import pytest
from watchdog.events import FileCreatedEvent

from app.core.daemon import (
    ContinuousWatchdogDaemon,
    DaemonFolderHandler,
    FileChangeEvent,
)


class DummySettings:
    """Mock configuration settings for testing event pipeline."""

    def __init__(self):
        self.LOG_FILE = "test.log"
        self.CONFLICT_POLICY = "rename"
        self.MAX_FOLDERS = 10
        self.STOP_WORDS = set()
        self.DEBOUNCE_DELAY = 0.1
        self.MAX_DEBOUNCE_DELAY = 1.0
        self.MAX_QUEUE_CAPACITY = 10
        self.DEDUP_WINDOW = 0.2
        self.MAX_WORKERS = 2
        self.RECONCILIATION_INTERVAL = 0.5
        self.IGNORED_EXTENSIONS = [".crdownload", ".tmp"]

    def load(self):
        pass


def test_file_change_event_structure():
    """Verify FileChangeEvent contains event_type, file_path, timestamp, and optional dest_path."""
    t0 = time.time()
    event = FileChangeEvent(event_type="created", file_path="/tmp/test.txt")

    assert event.event_type == "created"
    assert event.file_path == "/tmp/test.txt"
    assert event.timestamp >= t0
    assert event.dest_path is None

    moved_event = FileChangeEvent(
        event_type="moved",
        file_path="/tmp/test.txt",
        dest_path="/tmp/dest.txt",
    )
    assert moved_event.dest_path == "/tmp/dest.txt"


def test_watchdog_handler_publishes_to_queue(tmp_path):
    """Verify Watchdog event handlers publish FileChangeEvent directly to asynchronous FIFO queue."""
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    loop = asyncio.new_event_loop()
    daemon._event_loop = loop
    daemon._event_queue = asyncio.Queue(maxsize=10)

    handler = DaemonFolderHandler(daemon)
    test_file = tmp_path / "stream_doc.pdf"
    test_file.write_text("stream content")

    event = FileCreatedEvent(str(test_file))
    handler.on_any_event(event)

    assert daemon._event_queue.qsize() == 1
    enqueued = daemon._event_queue.get_nowait()
    assert isinstance(enqueued, FileChangeEvent)
    assert enqueued.file_path == str(test_file)
    assert enqueued.event_type == "created"

    loop.close()


def test_event_pipeline_deduplication(tmp_path):
    """Verify deduplication discards redundant intermediate events for active or recent paths."""
    settings = DummySettings()
    settings.DEDUP_WINDOW = 0.5
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    daemon._event_queue = asyncio.Queue(maxsize=10)

    file1 = str(tmp_path / "burst_file.txt")
    e1 = FileChangeEvent(event_type="created", file_path=file1)
    e2 = FileChangeEvent(event_type="modified", file_path=file1)

    res1 = daemon.enqueue_event(e1)
    assert res1 is True
    assert daemon._event_queue.qsize() == 1

    res2 = daemon.enqueue_event(e2)
    assert res2 is False
    assert daemon._event_queue.qsize() == 1


def test_event_pipeline_max_queue_capacity(tmp_path):
    """Verify in-memory queue enforces maximum capacity to prevent unbounded memory growth."""
    settings = DummySettings()
    settings.MAX_QUEUE_CAPACITY = 3
    settings.DEDUP_WINDOW = 0.0

    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True
    daemon._event_queue = asyncio.Queue(maxsize=3)

    for i in range(3):
        ev = FileChangeEvent(event_type="created", file_path=str(tmp_path / f"file_{i}.txt"))
        assert daemon.enqueue_event(ev) is True

    assert daemon._event_queue.full() is True

    overflow_ev = FileChangeEvent(event_type="created", file_path=str(tmp_path / "overflow.txt"))
    assert daemon.enqueue_event(overflow_ev) is False
    assert daemon._event_queue.qsize() == 3


@pytest.mark.anyio
async def test_per_path_locks_prevent_concurrent_collisions(tmp_path):
    """Verify worker tasks synchronize path locks so independent files run concurrently while same path is locked."""
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    path_a = str(tmp_path / "a.txt")
    path_b = str(tmp_path / "b.txt")

    lock_a1 = daemon._get_path_lock(os.path.normcase(path_a))
    lock_a2 = daemon._get_path_lock(os.path.normcase(path_a))
    lock_b = daemon._get_path_lock(os.path.normcase(path_b))

    assert lock_a1 is lock_a2
    assert lock_a1 is not lock_b


@pytest.mark.anyio
async def test_targeted_triage_without_whole_directory_scans(tmp_path):
    """Verify async worker tasks consume queued events and execute targeted triage without full-directory scans."""
    file_a = tmp_path / "target_a.pdf"
    file_a.write_text("target pdf content")

    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    mock_session = mock.MagicMock()
    mock_session.generate_sorting_plan.side_effect = [
        {"target_a.pdf": "sorted/target_a.pdf"},
        {},
    ]
    mock_session.execute_moves.return_value = {"moved": 1}

    daemon._get_or_create_session = mock.MagicMock(return_value=mock_session)

    with (
        mock.patch("app.core.daemon.get_files_recursively") as mock_scan,
        mock.patch("app.core.daemon.MetadataPass.run", return_value=[]) as mock_meta,
    ):
        event = FileChangeEvent(event_type="created", file_path=str(file_a))
        await daemon._process_single_event(event)

        # Whole directory scanner MUST NOT have been invoked by targeted worker triage!
        mock_scan.assert_not_called()
        mock_meta.assert_called_once()
        meta_args = mock_meta.call_args[0]
        # MetadataPass should be called specifically for ["target_a.pdf"]
        assert meta_args[1] == ["target_a.pdf"]


@pytest.mark.anyio
async def test_background_reconciliation_audit(tmp_path):
    """Verify periodic background reconciliation audit enqueues untracked files during idle period."""
    untracked_file = tmp_path / "untracked.pdf"
    untracked_file.write_text("untracked document content")

    settings = DummySettings()
    settings.RECONCILIATION_INTERVAL = 0.1
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True
    daemon._event_queue = asyncio.Queue(maxsize=10)

    # Execute reconciliation audit
    await daemon._run_reconciliation_audit()

    assert daemon._event_queue.qsize() == 1
    enqueued = daemon._event_queue.get_nowait()
    assert enqueued.event_type == "reconciliation"
    assert os.path.basename(enqueued.file_path) == "untracked.pdf"
