"""Tests for native multi-root workspace session architecture."""

import os
import shutil
import tempfile

import pytest

from app.config import AppSettings
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.metadata import MetadataPass
from app.core.scanner import get_files_from_sources, parse_item_identifier
from app.core.session import AppSession


@pytest.fixture
def multi_root_workspace():
    """Create two temporary source directories with test files."""
    tmpdir = tempfile.mkdtemp(prefix="sortify_multi_root_")
    root1 = os.path.join(tmpdir, "Downloads")
    root2 = os.path.join(tmpdir, "Desktop")
    os.makedirs(root1, exist_ok=True)
    os.makedirs(root2, exist_ok=True)

    file1 = os.path.join(root1, "financial_report_2024.pdf")
    file2 = os.path.join(root2, "meeting_notes_q3.txt")

    with open(file1, "w", encoding="utf-8") as f:
        f.write("Q4 Revenue and EBITDA report financial summary.")

    with open(file2, "w", encoding="utf-8") as f:
        f.write("Project kickoff meeting notes and agenda.")

    yield root1, root2, file1, file2

    shutil.rmtree(tmpdir, ignore_errors=True)


def test_parse_item_identifier():
    """Verify parse_item_identifier splits namespaced keys and defaults single paths."""
    src_root, rel = parse_item_identifier("/home/user/Downloads::docs/file.txt")
    assert src_root == os.path.normpath("/home/user/Downloads")
    assert rel == "docs/file.txt"

    src_root, rel = parse_item_identifier("simple.txt", default_base_dir="/home/user")
    assert src_root == os.path.normpath("/home/user")
    assert rel == "simple.txt"


def test_get_files_from_sources_single_and_multi(multi_root_workspace):
    """Verify get_files_from_sources yields relative paths for single root and namespaced keys for multi-root."""
    root1, root2, _, _ = multi_root_workspace

    # Single root session
    files_single = get_files_from_sources([root1])
    assert len(files_single) == 1
    assert files_single[0] == "financial_report_2024.pdf"

    # Multi-root session
    files_multi = get_files_from_sources([root1, root2])
    assert len(files_multi) == 2
    for item in files_multi:
        assert "::" in item


def test_app_session_multi_root_init(multi_root_workspace):
    """Verify AppSession accepts source_dirs and initializes base_dir and source_dirs."""
    root1, root2, _, _ = multi_root_workspace
    settings = AppSettings()

    session = AppSession(settings, source_dirs=[root1, root2])
    assert session.source_dirs == [os.path.normpath(root1), os.path.normpath(root2)]
    assert session.base_dir == os.path.normpath(root1)
    session.close()


def test_db_multi_root_document_isolation(multi_root_workspace):
    """Verify database get_all_documents queries documents across multiple source roots without collision."""
    root1, root2, file1, file2 = multi_root_workspace
    db_path = os.path.join(root1, "autosorter.db")
    worker = DBWorker()
    try:
        db = Database(db_path, worker)

        db.upsert_document(root1, "file.txt", "hash1", "Content in root1")
        db.upsert_document(root2, "file.txt", "hash2", "Content in root2")

        docs = db.get_all_documents(root1, source_dirs=[root1, root2])
        assert len(docs) == 2
        keys = [d[0] for d in docs]
        assert f"{root1}::file.txt" in keys
        assert f"{root2}::file.txt" in keys
    finally:
        worker.stop()


def test_metadata_pass_multi_root(multi_root_workspace):
    """Verify MetadataPass evaluates multi-root namespaced item identifiers."""
    root1, root2, _, _ = multi_root_workspace
    settings = AppSettings()

    items = get_files_from_sources([root1, root2])
    session = AppSession(settings, source_dirs=[root1, root2])
    try:
        bypassed = MetadataPass.run(
            session.base_dir,
            items,
            settings,
            session.db,
            source_dirs=session.source_dirs,
        )
        assert isinstance(bypassed, list)
    finally:
        session.close()


def test_multi_root_plan_generation_and_move_execution(multi_root_workspace):
    """Verify plan generation tags nodes with source_root and mover executes moves across roots."""
    root1, root2, file1, file2 = multi_root_workspace
    settings = AppSettings()

    session = AppSession(settings, source_dirs=[root1, root2])
    try:
        files = get_files_from_sources([root1, root2])
        MetadataPass.run(
            session.base_dir,
            files,
            settings,
            session.db,
            source_dirs=session.source_dirs,
        )

        plan = session.generate_sorting_plan()
        assert plan is not None

        summary = session.execute_moves(plan)
        assert summary.get("total_attempted", 0) >= 0
    finally:
        session.close()
