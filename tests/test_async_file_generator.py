from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.extractor import build_corpus_generator_async, get_file_hash
from app.core.session import AppSession
from app.ui.app import AutoSorterApp


@pytest.mark.anyio
async def test_build_corpus_generator_async_sequential(tmp_path):
    # Setup test files
    base_dir = tmp_path / "test_base"
    base_dir.mkdir()

    file1 = base_dir / "file1.txt"
    file1.write_text("Hello World!")

    file2 = base_dir / "file2.txt"
    file2.write_text("Some other text content.")

    db_path = tmp_path / "test.db"
    db_worker = DBWorker()
    db = Database(db_path, db_worker)

    settings = AppSettings()

    # 1. Test sequential file-by-file extraction
    gen = build_corpus_generator_async(
        base_dir=str(base_dir),
        items_to_sort=["file1.txt", "file2.txt"],
        db=db,
        settings=settings,
    )

    results = []
    async for item, text, fhash, was_skipped in gen:
        results.append((item, text, fhash, was_skipped))

    assert len(results) == 2
    assert results[0][0] == "file1.txt"
    assert "Hello World!" in results[0][1]
    assert results[0][3] is False  # extracted, not skipped

    assert results[1][0] == "file2.txt"
    assert "Some other text content." in results[1][1]
    assert results[1][3] is False


@pytest.mark.anyio
async def test_build_corpus_generator_async_cache_skip(tmp_path):
    # Setup test file
    base_dir = tmp_path / "test_base"
    base_dir.mkdir()

    file1 = base_dir / "file1.txt"
    file1.write_text("Cached text")

    db_path = tmp_path / "test.db"
    db_worker = DBWorker()
    db = Database(db_path, db_worker)

    # Pre-populate database with matching hash
    fhash = get_file_hash(str(file1))
    db.upsert_document(
        str(base_dir), "file1.txt", fhash, "This is extracted cached text!"
    )

    settings = AppSettings()

    # Test that extraction is skipped
    gen = build_corpus_generator_async(
        base_dir=str(base_dir), items_to_sort=["file1.txt"], db=db, settings=settings
    )

    results = []
    async for item, text, h, was_skipped in gen:
        results.append((item, text, h, was_skipped))

    assert len(results) == 1
    assert results[0][0] == "file1.txt"
    assert results[0][1] == "This is extracted cached text!"
    assert results[0][2] == fhash
    assert results[0][3] is True  # was_skipped must be True


@pytest.mark.anyio
async def test_build_corpus_generator_async_cancellation(tmp_path):
    # Setup test files
    base_dir = tmp_path / "test_base"
    base_dir.mkdir()

    file1 = base_dir / "file1.txt"
    file1.write_text("File 1")
    file2 = base_dir / "file2.txt"
    file2.write_text("File 2")

    db_path = tmp_path / "test.db"
    db_worker = DBWorker()
    db = Database(db_path, db_worker)

    settings = AppSettings()

    cancel_flag = False

    def cancel_check():
        return cancel_flag

    gen = build_corpus_generator_async(
        base_dir=str(base_dir),
        items_to_sort=["file1.txt", "file2.txt"],
        db=db,
        cancel_check=cancel_check,
        settings=settings,
    )

    results = []
    async for item, text, h, was_skipped in gen:
        results.append((item, text, h, was_skipped))
        cancel_flag = True  # Cancel after yielding first file

    assert len(results) == 1
    assert results[0][0] == "file1.txt"


@pytest.mark.anyio
async def test_process_items_async_wrapping(tmp_path):
    base_dir = tmp_path / "test_base"
    base_dir.mkdir()

    file1 = base_dir / "file1.txt"
    file1.write_text("Wrappy wrap")

    settings = AppSettings()
    session = AppSession(settings, str(base_dir))

    gen = session.process_items_async(["file1.txt"], lambda: False)

    results = []
    async for res in gen:
        results.append(res)

    assert len(results) == 1
    assert results[0][0] == "file1.txt"
    assert "Wrappy wrap" in results[0][1]
    assert results[0][3] is False

    session.close()


@pytest.mark.anyio
async def test_scan_and_process_worker_ui_updates(tmp_path):
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False

    base_dir = tmp_path / "test_base"
    base_dir.mkdir()

    app = AutoSorterApp(settings)
    app.base_dir = str(base_dir)
    app.app_session = MagicMock()
    app.progress_bar = MagicMock()
    app.status_label = MagicMock()
    app.cancel_btn = MagicMock()
    app.execute_btn = MagicMock()

    # Mock files
    files = ["file1.txt", "file2.txt"]

    # Define mock async generator for process_items_async
    async def mock_generator(items, cancel_check):
        yield "file1.txt", "content1", "hash1", False
        yield "file2.txt", "content2", "hash2", True

    app.app_session.process_items_async = mock_generator

    with (
        patch("app.core.scanner.get_files_recursively", return_value=files),
        patch("app.core.metadata.MetadataPass.run", return_value=[]),
        patch("app.core.verifier.is_ml_available", return_value=False),
        patch("asyncio.sleep", return_value=None),
    ):
        await app._scan_and_process_worker()

        # Verify ui updates
        assert app.total_files == 2
        assert app.completed_files == 2

        # Check that status label updated sequentially
        status_calls = [c[0][0] for c in app.status_label.set_text.call_args_list]
        assert any("Processed 1/2 files" in call for call in status_calls)
        assert any("Processed 2/2 files" in call for call in status_calls)
        assert any("skipped unchanged: file2.txt" in call for call in status_calls)
