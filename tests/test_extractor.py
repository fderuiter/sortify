import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.extractor import (
    build_corpus_generator,
    extract_file_text,
    process_item_worker,
)
from app.core.history import HistoryManager

_test_dir = None
db_worker = None
db = None
cache_manager = None
history_manager = None

def setup_module(module):
    global _test_dir, db_worker, db, cache_manager, history_manager
    _test_dir = tempfile.mkdtemp()
    db_worker = DBWorker()
    db = Database(Path(_test_dir) / "test.db", db_worker)
    cache_manager = CacheManager(str(Path(_test_dir) / "cache.db"), db_worker)
    history_manager = HistoryManager(db, cache_manager, str(Path(_test_dir) / "history.db"))

def teardown_module(module):
    global _test_dir, db_worker
    if db_worker:
        db_worker.stop()
    import shutil
    if _test_dir:
        shutil.rmtree(_test_dir, ignore_errors=True)

def save_cache_sync(*args, **kwargs):
    cache_manager.save_cache_sync(*args, **kwargs)


@pytest.fixture
def mock_txt_file(mocker):
    mocker.patch("os.path.isfile", return_value=True)
    mocker.patch("os.path.splitext", return_value=("file", ".txt"))
    return mocker.patch(
        "builtins.open", mocker.mock_open(read_data="Sample text content.")
    )


def test_extract_txt(mock_txt_file):
    text = extract_file_text("dummy.txt")
    assert text == "Sample text content."


def test_extract_docx(mocker):
    mocker.patch("os.path.splitext", return_value=("file", ".docx"))
    mock_doc = mocker.patch("app.core.extractor_strategies.Document")
    mock_instance = mock_doc.return_value
    mock_instance.paragraphs = [
        MagicMock(text="Paragraph 1"),
        MagicMock(text="Paragraph 2"),
    ]

    text = extract_file_text("dummy.docx")
    assert text == "Paragraph 1\nParagraph 2"


def test_extract_csv(mocker):
    mocker.patch("os.path.splitext", return_value=("file", ".csv"))
    mocker.patch("builtins.open", mocker.mock_open(read_data="col1,col2\nval1,val2"))
    mocker.patch(
        "app.core.extractor_strategies.csv.reader",
        return_value=[["col1", "col2"], ["val1", "val2"]],
    )

    text = extract_file_text("dummy.csv")
    assert text == "col1 col2 val1 val2"


def test_extract_excel(mocker):
    mocker.patch("os.path.splitext", return_value=("file", ".xlsx"))
    mock_pd = mocker.patch("app.core.extractor_strategies.pd.read_excel")
    mock_df = mock_pd.return_value
    mock_df.to_string.return_value = "Excel content"

    text = extract_file_text("dummy.xlsx")
    assert text == "Excel content"


def test_extract_pdf(mocker):
    mocker.patch("os.path.splitext", return_value=("file", ".pdf"))
    mocker.patch("builtins.open", mocker.mock_open())
    mock_pdf = mocker.patch("app.core.extractor_strategies.pypdf.PdfReader")
    mock_instance = mock_pdf.return_value
    mock_page = MagicMock()
    mock_page.extract_text.return_value = "PDF text"
    mock_instance.pages = [mock_page]

    text = extract_file_text("dummy.pdf")
    assert text == "PDF text"


def test_extract_unsupported(mocker):
    mocker.patch("os.path.splitext", return_value=("file", ".unknown"))
    text = extract_file_text("dummy.unknown")
    assert text == "[STATUS:UNSUPPORTED]"


def test_process_item_worker_file(mocker):
    mocker.patch("os.path.isfile", return_value=True)
    mocker.patch("app.core.extractor.get_file_hash", return_value="hash1")
    mocker.patch.object(db, "get_document", return_value=None)
    mocker.patch("app.core.extractor.extract_file_text", return_value="worker text")

    mock_callback = MagicMock()
    item, text, fhash = process_item_worker("/base", "file.txt", mock_callback, db)

    assert item == "file.txt"
    assert text == "worker text"
    assert fhash == "hash1"
    mock_callback.assert_called_once()


def test_process_item_worker_dir(mocker):
    mocker.patch("os.path.isfile", return_value=False)
    mocker.patch("os.path.isdir", return_value=True)

    mock_callback = MagicMock()
    item, text, fhash = process_item_worker("/base", "subdir", mock_callback, db)

    assert item == "subdir"
    assert text == "subdir"
    assert fhash == ""
    mock_callback.assert_called_once()


def test_process_item_worker_exception(mocker):
    mocker.patch("os.path.isfile", side_effect=Exception("Test error"))
    mock_logger = mocker.patch("app.core.extractor.logging.error")

    mock_callback = MagicMock()
    item, text, fhash = process_item_worker("/base", "error.txt", mock_callback, db)

    assert item == "error.txt"
    assert text == ""
    assert fhash == ""
    mock_logger.assert_called_once()
    mock_callback.assert_called_once()


def test_build_corpus_generator(mocker):
    mocker.patch(
        "app.core.extractor.process_item_worker",
        side_effect=[
            ("file1.txt", "text1", "h1"),
            ("file2.txt", "text2", "h2"),
            ("file3.txt", "text3", "h3"),
        ],
    )
    mocker.patch.object(db, "get_document", return_value=None)

    mock_callback = MagicMock()
    generator = build_corpus_generator(
        "/base",
        ["file1.txt", "file2.txt", "file3.txt"],
        mock_callback,
        max_workers=2,
        chunk_size=2, db=db
    )

    chunks = list(generator)
    assert len(chunks) == 2
    assert "file1.txt" in chunks[0] or "file1.txt" in chunks[1]
    assert len(chunks[0]) == 2
    assert len(chunks[1]) == 1

def test_build_corpus_generator_model_mismatch(mocker):
    mocker.patch(
        "app.core.extractor.process_item_worker",
        return_value=("file1.txt", "cached_text", "hash1")
    )
    # Simulate DB having a different model name
    db.get_document = mocker.MagicMock(return_value={
        "file_hash": "hash1", 
        "embedding": b"fake_embedding",
        "model_name": "old-model",
        "vector_dimension": 384
    })
    
    mock_callback = MagicMock()
    # 1. Matching model (should skip)
    gen_match = build_corpus_generator(
        "/base", ["file1.txt"], mock_callback, max_workers=1, db=db, chunk_size=2,
        active_model_name="old-model", active_dimension=384
    )
    assert len(list(gen_match)) == 0
    
    # 2. Mismatched model (should yield text for re-embedding)
    gen_mismatch = build_corpus_generator(
        "/base", ["file1.txt"], mock_callback, max_workers=1, db=db, chunk_size=2,
        active_model_name="new-model", active_dimension=768
    )
    chunks = list(gen_mismatch)
    assert len(chunks) == 1
    assert "file1.txt" in chunks[0]
    assert chunks[0]["file1.txt"]["text"] == "file1.txt cached_text"
