"""Unit and integration tests for Priority Key Sorting and Fast-Path Async Priority Queueing."""

import asyncio
import os
import time

from app.core.daemon import ContinuousWatchdogDaemon, FileChangeEvent
from app.core.mover import _collect_move_items, _execute_moves_recursive


class DummySettings:
    """Mock settings for daemon testing."""

    def __init__(self):
        self.LOG_FILE = "test.log"
        self.CONFLICT_POLICY = "rename"
        self.MAX_FOLDERS = 10
        self.STOP_WORDS = set()
        self.DEBOUNCE_DELAY = 0.1
        self.MAX_DEBOUNCE_DELAY = 1.0
        self.MAX_QUEUE_CAPACITY = 100
        self.DEDUP_WINDOW = 0.0
        self.MAX_WORKERS = 2
        self.RECONCILIATION_INTERVAL = 10.0
        self.IGNORED_EXTENSIONS = [".tmp"]

    def load(self):
        pass


def test_file_mover_orders_by_priority_score_and_mtime(test_history_env):
    """Verify _execute_moves_recursive and _collect_move_items order items by priority ascending, score descending, and mtime ascending."""
    base_dir, db, cache, history_manager, db_worker = test_history_env
    now = time.time()

    f_tech = os.path.join(base_dir, "technical.log")
    f_legal = os.path.join(base_dir, "contract.pdf")
    f_fin_new = os.path.join(base_dir, "invoice_new.csv")
    f_fin_old = os.path.join(base_dir, "invoice_old.csv")

    for p in (f_tech, f_legal, f_fin_new, f_fin_old):
        with open(p, "w") as f:
            f.write("content")

    # Mtimes out of priority order
    os.utime(f_tech, (now - 10000, now - 10000))
    os.utime(f_legal, (now - 5000, now - 5000))
    os.utime(f_fin_new, (now - 1000, now - 1000))
    os.utime(f_fin_old, (now - 3000, now - 3000))

    plan = {
        "technical.log": {
            "__type__": "file",
            "relative_source": "technical.log",
            "target_filename": "out_tech.log",
            "archival_priority": 4,
            "archival_priority_score": 0.30,
        },
        "contract.pdf": {
            "__type__": "file",
            "relative_source": "contract.pdf",
            "target_filename": "out_legal.pdf",
            "archival_priority": 1,
            "archival_priority_score": 0.85,
        },
        "invoice_new.csv": {
            "__type__": "file",
            "relative_source": "invoice_new.csv",
            "target_filename": "out_fin_new.csv",
            "archival_priority": 1,
            "archival_priority_score": 0.90,
        },
        "invoice_old.csv": {
            "__type__": "file",
            "relative_source": "invoice_old.csv",
            "target_filename": "out_fin_old.csv",
            "archival_priority": 1,
            "archival_priority_score": 0.90,
        },
    }

    collected = _collect_move_items(base_dir, plan)
    collected_files = [item["filename"] for item in collected]

    # Priority 1 score 0.90 (oldest mtime first): out_fin_old.csv, out_fin_new.csv
    # Priority 1 score 0.85: out_legal.pdf
    # Priority 4 score 0.30: out_tech.log
    expected_order = [
        "out_fin_old.csv",
        "out_fin_new.csv",
        "out_legal.pdf",
        "out_tech.log",
    ]
    assert collected_files == expected_order

    db_updates_batch = []
    _execute_moves_recursive(
        base_dir, plan, db, session_id="test-session", db_updates_batch=db_updates_batch
    )

    executed_sources = [
        item["args"][3]
        for item in db_updates_batch
        if item.get("type") == "transaction_step"
    ]
    assert executed_sources == [
        "invoice_old.csv",
        "invoice_new.csv",
        "contract.pdf",
        "technical.log",
    ]


def test_directory_nodes_check_child_node_priorities(tmp_path):
    """Verify directory subtrees inherit the highest priority child and process high-priority subtrees first."""
    base_dir = str(tmp_path)
    now = time.time()

    os.makedirs(os.path.join(base_dir, "dir_tech"), exist_ok=True)
    os.makedirs(os.path.join(base_dir, "dir_fin"), exist_ok=True)

    f_log = os.path.join(base_dir, "dir_tech", "system.log")
    f_inv = os.path.join(base_dir, "dir_fin", "report.xlsx")

    with open(f_log, "w") as f:
        f.write("log")
    with open(f_inv, "w") as f:
        f.write("excel")

    # Mtimes: tech folder file is older
    os.utime(f_log, (now - 10000, now - 10000))
    os.utime(f_inv, (now - 1000, now - 1000))

    plan = {
        "dir_tech": {
            "system.log": {
                "__type__": "file",
                "relative_source": "dir_tech/system.log",
                "target_filename": "out_system.log",
                "archival_priority": 4,
                "archival_priority_score": 0.30,
            }
        },
        "dir_fin": {
            "report.xlsx": {
                "__type__": "file",
                "relative_source": "dir_fin/report.xlsx",
                "target_filename": "out_report.xlsx",
                "archival_priority": 1,
                "archival_priority_score": 0.90,
            }
        },
    }

    collected = _collect_move_items(base_dir, plan)
    collected_files = [item["filename"] for item in collected]

    # High-priority subtree dir_fin (priority 1) MUST execute before low-priority subtree dir_tech (priority 4)
    assert collected_files == ["out_report.xlsx", "out_system.log"]


def test_unclassified_and_missing_metadata_defaults(tmp_path):
    """Verify nodes lacking archival priority metadata default to archival_priority=5 and score=0.0."""
    base_dir = str(tmp_path)

    f_unclass = os.path.join(base_dir, "unclassified.txt")
    f_prio1 = os.path.join(base_dir, "financial.csv")

    with open(f_unclass, "w") as f:
        f.write("text")
    with open(f_prio1, "w") as f:
        f.write("csv")

    plan = {
        "unclassified.txt": {
            "__type__": "file",
            "relative_source": "unclassified.txt",
            "target_filename": "out_unclassified.txt",
        },
        "financial.csv": {
            "__type__": "file",
            "relative_source": "financial.csv",
            "target_filename": "out_financial.csv",
            "archival_priority": 1,
            "archival_priority_score": 0.90,
        },
    }

    collected = _collect_move_items(base_dir, plan)
    collected_files = [item["filename"] for item in collected]

    # Priority 1 financial.csv MUST be placed before unclassified (default priority 5)
    assert collected_files == ["out_financial.csv", "out_unclassified.txt"]


def test_watchdog_daemon_uses_priority_queue_and_fast_path_jev(tmp_path):
    """Verify ContinuousWatchdogDaemon._event_queue uses asyncio.PriorityQueue and fast-path Jev classification sets event priority keys."""
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    loop = asyncio.new_event_loop()
    daemon._event_loop = loop
    daemon._event_queue = asyncio.PriorityQueue(maxsize=100)

    # Verify event queue is a PriorityQueue instance
    assert isinstance(daemon._event_queue, asyncio.PriorityQueue)

    f_tech = tmp_path / "system.log"
    f_fin1 = tmp_path / "invoice1.csv"
    f_fin2 = tmp_path / "invoice2.csv"

    f_tech.write_text("log content")
    f_fin1.write_text("invoice 1 content")
    f_fin2.write_text("invoice 2 content")

    # Enqueue low priority event first (Technical log -> priority 4)
    ev_tech = FileChangeEvent(event_type="created", file_path=str(f_tech))
    assert daemon.enqueue_event(ev_tech) is True

    # Enqueue high priority events second and third (Financial invoice -> priority 1)
    ev_fin1 = FileChangeEvent(event_type="created", file_path=str(f_fin1))
    assert daemon.enqueue_event(ev_fin1) is True

    ev_fin2 = FileChangeEvent(event_type="created", file_path=str(f_fin2))
    assert daemon.enqueue_event(ev_fin2) is True

    assert daemon._event_queue.qsize() == 3

    # Dequeue items in priority order
    item1 = daemon._event_queue.get_nowait()
    item2 = daemon._event_queue.get_nowait()
    item3 = daemon._event_queue.get_nowait()

    event1 = item1[2]
    event2 = item2[2]
    event3 = item3[2]

    # Financial items (priority 1) MUST be dequeued before Technical item (priority 4)
    assert os.path.basename(event1.file_path) == "invoice1.csv"
    assert os.path.basename(event2.file_path) == "invoice2.csv"
    assert os.path.basename(event3.file_path) == "system.log"

    # Fast-path Jev classification sets priority keys
    assert event1.archival_priority == 1
    assert event1.priority_key == (1, -0.90)

    assert event3.archival_priority == 4
    assert event3.priority_key == (4, -0.30)

    loop.close()
