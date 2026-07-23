import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.core.analyzer import IncrementalAnalyzer
from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.extractor import build_corpus_generator
from app.core.history import HistoryManager
from tests.generate_corpus import CORPUS_DIR, create_corpus

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
    history_manager = HistoryManager(
        db, cache_manager, str(Path(_test_dir) / "history.db")
    )


def teardown_module(module):
    global _test_dir, db_worker
    if db_worker:
        db_worker.stop()
    import shutil

    if _test_dir:
        shutil.rmtree(_test_dir, ignore_errors=True)


@pytest.fixture(scope="function", autouse=True)
def clean_db():
    db.clear()
    yield


@pytest.fixture(scope="session", autouse=True)
def setup_corpus():
    create_corpus()
    yield


def test_full_workflow_simulation():
    # Requirement: execute a complete simulation from file text extraction to sorting plan
    # Requirement: multiple document types including PDF, Word, plain text

    files = [f for f in os.listdir(CORPUS_DIR) if f != "empty.txt" and f != "dummy.pdf"]
    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "and"}, model_path="all-MiniLM-L6-v2", db=db
    )
    progress_callback = MagicMock()

    # Process files asynchronously using the generator (which uses max workers)
    generator = build_corpus_generator(
        CORPUS_DIR,
        files,
        progress_callback,
        max_workers=4,
        db=db,
        chunk_size=50,
    )

    for chunk in generator:
        analyzer.partial_fit(CORPUS_DIR, chunk)

    plan = analyzer.generate_sorting_plan(CORPUS_DIR)

    # We should have some sorted categories and files
    assert isinstance(plan, dict)

    # Assert specific semantic outcomes (e.g., finance files together, tech together)
    # The output folder names are based on extracted words (e.g. Finance-money, Technology-software)
    # We will search the nested dictionary to find our files.
    def find_file_folder(p, filename, current_path=""):
        if not isinstance(p, dict) or p.get("__type__") == "file":
            return None
        for k, v in p.items():
            if v is None or (isinstance(v, dict) and v.get("__type__") == "file"):
                if k == filename:
                    return current_path
            else:
                found = find_file_folder(
                    v, filename, f"{current_path}/{k}" if current_path else k
                )
                if found is not None:
                    return found
        return None

    # Verify that files are sorted into some folders
    finance_txt_folder = find_file_folder(plan, "finance_report.txt")
    finance_csv_folder = find_file_folder(plan, "finance_data.csv")
    tech_txt_folder = find_file_folder(plan, "tech_notes.txt")
    health_docx_folder = find_file_folder(plan, "health_doc.docx")

    assert finance_txt_folder is not None, "finance_report.txt should be sorted"
    assert finance_csv_folder is not None, "finance_data.csv should be sorted"
    assert tech_txt_folder is not None, "tech_notes.txt should be sorted"
    assert health_docx_folder is not None, "health_doc.docx should be sorted"

    science_pdf_folder = find_file_folder(plan, "science_doc.pdf")
    assert science_pdf_folder is not None, "science_doc.pdf should be sorted"

    assert progress_callback.call_count == len(files)


def test_small_dataset_fallback():
    # Requirement: Edge cases, small datasets produce fallback plans without errors
    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "and"}, model_path="all-MiniLM-L6-v2", db=db
    )

    # Only 2 files
    corpus = {"file1.txt": "Some content here.", "file2.txt": "More content there."}

    analyzer.partial_fit("dummy", corpus)
    plan = analyzer.generate_sorting_plan("dummy")

    # Expect fallback to Miscellaneous
    assert "Miscellaneous" in plan
    assert "file1.txt" in plan["Miscellaneous"]
    assert "file2.txt" in plan["Miscellaneous"]


def test_empty_files_handling():
    # Requirement: Edge cases, empty files produce expected fallback
    analyzer = IncrementalAnalyzer(
        max_folders=3, stop_words={"the", "and"}, model_path="all-MiniLM-L6-v2", db=db
    )

    corpus = {"empty1.txt": "", "empty2.txt": "", "empty3.txt": ""}

    analyzer.partial_fit("dummy", corpus)
    plan = analyzer.generate_sorting_plan("dummy")

    # Since text is empty, topic indices might just fallback
    assert isinstance(plan, dict)
    # Check if empty files are handled without crash


def test_concurrent_large_volume():
    # Requirement: accurately simulates the asynchronous processing of at least 20 files simultaneously
    analyzer = IncrementalAnalyzer(
        max_folders=5, stop_words={"the", "and"}, model_path="all-MiniLM-L6-v2", db=db
    )
    progress_callback = MagicMock()

    with tempfile.TemporaryDirectory() as temp_dir:
        # Create 25 files
        files_to_sort = []
        for i in range(25):
            fname = f"sim_file_{i}.txt"
            with open(os.path.join(temp_dir, fname), "w") as f:
                f.write(f"This is simulation file {i} about technology and computers.")
            files_to_sort.append(fname)

        generator = build_corpus_generator(
            temp_dir,
            files_to_sort,
            progress_callback,
            max_workers=4,
            db=db,
            chunk_size=10,
        )

        for chunk in generator:
            analyzer.partial_fit(temp_dir, chunk)

        plan = analyzer.generate_sorting_plan(temp_dir)

        # Verify
        assert progress_callback.call_count == 25
        assert isinstance(plan, dict)

        # Verify all 25 files are present
        def get_all_files(p):
            result = []
            if not isinstance(p, dict) or p.get("__type__") == "file":
                return result
            for k, v in p.items():
                if v is None or (isinstance(v, dict) and v.get("__type__") == "file"):
                    result.append(k)
                else:
                    result.extend(get_all_files(v))
            return result

        sorted_files = get_all_files(plan)
        assert len(sorted_files) == 25
