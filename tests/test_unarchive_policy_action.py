"""Unit and integration tests for rule-driven unarchive policy action."""

import os
import zipfile
from types import SimpleNamespace

import pytest

from app.core.daemon import ContinuousWatchdogDaemon
from app.core.db import Database
from app.core.db_conn import clear_connection_cache
from app.core.db_worker import DBWorker
from app.core.policy_engine import PolicyEngine
from app.core.quarantine_interceptor import QuarantineInterceptorService


def create_sample_zip(zip_path: str, files_dict: dict[str, str]) -> None:
    """Helper to create a zip file at zip_path with file contents from files_dict."""
    os.makedirs(os.path.dirname(zip_path), exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fname, content in files_dict.items():
            zf.writestr(fname, content)


def test_policy_engine_unarchive_action():
    """Test PolicyEngine support for unarchive in SUPPORTED_ACTIONS and rule matching."""
    assert PolicyEngine.validate_policy_action("unarchive") is True
    assert "unarchive" in PolicyEngine.SUPPORTED_ACTIONS

    policies = [
        {
            "type": "keyword",
            "expression": "project",
            "action": "unarchive",
            "target_path": "Extracted_Projects",
            "priority": 100,
        }
    ]

    rule = PolicyEngine.evaluate_policies("project_alpha.zip", "", "", policies)
    assert rule is not None
    assert rule.action == "unarchive"
    assert rule.target_path == "Extracted_Projects"


def test_quarantine_interceptor_unarchive_default_delete(tmp_path):
    """Test QuarantineInterceptorService extracting zip archive and deleting source archive by default."""
    base_dir = str(tmp_path)
    worker = DBWorker()
    db = Database(os.path.join(base_dir, "autosorter.db"), worker)

    try:
        # Create incoming zip archive
        zip_name = "project_docs.zip"
        zip_path = os.path.join(base_dir, zip_name)
        create_sample_zip(
            zip_path,
            {
                "invoice_101.pdf": "Invoice total $1000 for project docs",
                "notes.txt": "Project alpha meeting notes",
            },
        )

        policies = [
            {
                "type": "keyword",
                "expression": "project",
                "action": "unarchive",
                "target_path": "Unpacked",
                "priority": 100,
            }
        ]

        service = QuarantineInterceptorService(db=db, policies=policies)
        staged = service.stage_incoming_file(
            source_path=zip_path, base_dir=base_dir, original_relative_path=zip_name
        )

        rec = service.process_quarantine_job(staged["job_id"])

        assert rec.status == "UNARCHIVED"
        assert rec.policy_action == "unarchive"

        # Verify extracted files exist in dest_dir
        unpacked_dir = os.path.join(base_dir, "Unpacked")
        assert os.path.exists(os.path.join(unpacked_dir, "invoice_101.pdf"))
        assert os.path.exists(os.path.join(unpacked_dir, "notes.txt"))

        # Default post_unarchive_action is delete/remove: source archive should be deleted
        assert not os.path.exists(zip_path)
        assert not os.path.exists(staged["staged_filepath"])
    finally:
        worker.stop()
        clear_connection_cache()


def test_quarantine_interceptor_unarchive_move_source(tmp_path):
    """Test QuarantineInterceptorService relocating source archive to archive_target upon unarchiving."""
    base_dir = str(tmp_path)
    worker = DBWorker()
    db = Database(os.path.join(base_dir, "autosorter.db"), worker)

    try:
        zip_name = "project_beta.zip"
        zip_path = os.path.join(base_dir, zip_name)
        create_sample_zip(
            zip_path,
            {"summary.txt": "Project Beta summary details"},
        )

        policies = [
            {
                "type": "keyword",
                "expression": "beta",
                "action": "unarchive",
                "target_path": "Extracted_Beta",
                "post_unarchive_action": "move",
                "post_unarchive_target": "Processed_Archives",
                "priority": 100,
            }
        ]

        service = QuarantineInterceptorService(db=db, policies=policies)
        staged = service.stage_incoming_file(
            source_path=zip_path, base_dir=base_dir, original_relative_path=zip_name
        )

        rec = service.process_quarantine_job(staged["job_id"])

        assert rec.status == "UNARCHIVED"

        # Extracted files
        extracted_dir = os.path.join(base_dir, "Extracted_Beta")
        assert os.path.exists(os.path.join(extracted_dir, "summary.txt"))

        # Source archive relocated to Processed_Archives
        relocated_archive = os.path.join(base_dir, "Processed_Archives", zip_name)
        assert os.path.exists(relocated_archive)
        assert not os.path.exists(zip_path)
    finally:
        worker.stop()
        clear_connection_cache()


def test_quarantine_interceptor_unarchive_retain_source(tmp_path):
    """Test QuarantineInterceptorService retaining original source archive in place."""
    base_dir = str(tmp_path)
    worker = DBWorker()
    db = Database(os.path.join(base_dir, "autosorter.db"), worker)

    try:
        zip_name = "project_gamma.zip"
        zip_path = os.path.join(base_dir, zip_name)
        create_sample_zip(
            zip_path,
            {"readme.txt": "Project Gamma overview"},
        )

        policies = [
            {
                "type": "keyword",
                "expression": "gamma",
                "action": "unarchive",
                "target_path": "Extracted_Gamma",
                "post_unarchive_action": "retain",
                "priority": 100,
            }
        ]

        service = QuarantineInterceptorService(db=db, policies=policies)
        staged = service.stage_incoming_file(
            source_path=zip_path, base_dir=base_dir, original_relative_path=zip_name
        )

        rec = service.process_quarantine_job(staged["job_id"])

        assert rec.status == "UNARCHIVED"

        # Extracted file exists
        assert os.path.exists(os.path.join(base_dir, "Extracted_Gamma", "readme.txt"))

        # Original archive retained
        assert os.path.exists(zip_path)
    finally:
        worker.stop()
        clear_connection_cache()


def test_quarantine_interceptor_unarchive_unsafe_limits(tmp_path):
    """Test QuarantineInterceptorService aborting unsafe archive extractions and routing to DLQ."""
    base_dir = str(tmp_path)
    worker = DBWorker()
    db = Database(os.path.join(base_dir, "autosorter.db"), worker)

    try:
        zip_name = "zip_bomb.zip"
        zip_path = os.path.join(base_dir, zip_name)
        create_sample_zip(zip_path, {"huge.txt": "A" * (2 * 1024 * 1024)})  # 2MB string

        policies = [
            {
                "type": "keyword",
                "expression": "zip_bomb",
                "action": "unarchive",
                "target_path": "Unpacked",
                "priority": 100,
            }
        ]

        service = QuarantineInterceptorService(db=db, policies=policies)
        service.forensic_scanner.max_file_size_bytes = 100 * 1024  # 100KB limit

        staged = service.stage_incoming_file(
            source_path=zip_path, base_dir=base_dir, original_relative_path=zip_name
        )

        rec = service.process_quarantine_job(staged["job_id"])

        assert rec.status == "DEAD_LETTER_QUEUE"
        assert "Archive safety violation" in str(rec.error) or "Archive safety" in str(
            rec.get("error_message")
        )
    finally:
        worker.stop()
        clear_connection_cache()


@pytest.mark.anyio
async def test_daemon_unarchive_event_queue_triage(tmp_path):
    """Test ContinuousWatchdogDaemon unarchiving incoming zip and registering extracted child files into event queue."""
    base_dir = str(tmp_path)
    worker = DBWorker()
    db = Database(os.path.join(base_dir, "autosorter.db"), worker)

    class CustomSettings:
        def __init__(self):
            self.LOG_FILE = "autosorter.log"
            self.CONFLICT_POLICY = "rename"
            self.MAX_FOLDERS = 10
            self.STOP_WORDS = set()
            self.AI_CONSENT_GRANTED = False
            self.POLICIES = [
                {
                    "type": "keyword",
                    "expression": "archive_project",
                    "action": "unarchive",
                    "target_path": "",
                    "priority": 100,
                }
            ]
            self.KEYWORD_RULES = {"invoice": "Financials"}
            self.WORKER_TIMEOUT = 300.0

        def load(self):
            pass

        def _save(self):
            pass

    try:
        settings = CustomSettings()

        daemon = ContinuousWatchdogDaemon(settings=settings, base_dir=base_dir)
        daemon._is_running = True
        daemon._event_queue = __import__("asyncio").PriorityQueue()

        zip_name = "archive_project.zip"
        zip_path = os.path.join(base_dir, zip_name)
        create_sample_zip(
            zip_path,
            {"invoice_999.pdf": "Invoice for services rendered"},
        )

        session = SimpleNamespace(
            db=db,
            generate_sorting_plan=lambda **kwargs: None,
            execute_moves=lambda plan: {},
        )
        daemon._session = session

        await daemon._triage_file_path(zip_path)

        # Source archive deleted
        assert not os.path.exists(zip_path)

        # Extracted file exists
        extracted_pdf = os.path.join(base_dir, "invoice_999.pdf")
        assert os.path.exists(extracted_pdf)

        # Event queue registered child file
        assert daemon._event_queue.qsize() == 1
        _, _, enqueued_event = await daemon._event_queue.get()
        assert os.path.realpath(enqueued_event.file_path) == os.path.realpath(extracted_pdf)
    finally:
        worker.stop()
        clear_connection_cache()
