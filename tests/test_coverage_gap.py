import hashlib
import os
import tempfile
from pathlib import Path
from unittest import mock

import pypdf

from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.extractor import (
    build_corpus_generator,
    extract_file_text,
    get_file_hash,
    process_item_worker,
)
from app.core.history import HistoryManager
from app.core.mover import (
    _remove_empty_dirs,
    execute_moves,
    get_safe_path,
)

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
    from app.core.db_conn import clear_connection_cache

    clear_connection_cache()
    import shutil

    if _test_dir:
        shutil.rmtree(_test_dir, ignore_errors=True)


def test_get_file_hash_exception(tmp_path):
    with mock.patch("builtins.open", side_effect=PermissionError):
        h = get_file_hash(str(tmp_path / "nonexistent.txt"))
        assert h == hashlib.sha256().hexdigest()


def test_extract_file_text_status_empty(tmp_path):
    file_path = tmp_path / "dummy.txt"
    with open(file_path, "w") as f:
        f.write("Some data")

    with mock.patch(
        "app.core.extractor_strategies.TxtExtractor.extract", return_value="   "
    ):
        text = extract_file_text(str(file_path))
        assert text == "[STATUS:EMPTY]"


def test_extract_file_text_encrypted(tmp_path):
    with mock.patch("app.core.extractor_strategies.registry.get_extractor") as mock_get:
        mock_ext = mock.MagicMock()
        mock_ext.extract.side_effect = pypdf.errors.FileNotDecryptedError("test")
        mock_get.return_value = mock_ext
        text = extract_file_text("dummy.pdf")
        assert text == "[STATUS:ENCRYPTED]"


def test_extract_file_text_exception(tmp_path):
    with mock.patch("app.core.extractor_strategies.registry.get_extractor") as mock_get:
        mock_ext = mock.MagicMock()
        mock_ext.extract.side_effect = Exception("General Failure")
        mock_get.return_value = mock_ext
        text = extract_file_text("dummy.txt")
        assert text == "[STATUS:FAILED]"


def test_process_item_worker_already_processed(tmp_path):
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello")
    file_hash = get_file_hash(str(file_path))
    db.upsert_document(str(tmp_path), "test.txt", file_hash, "hello")

    cb = mock.MagicMock()
    item, text, h = process_item_worker(str(tmp_path), "test.txt", cb, db)
    assert text == "hello"
    assert h == file_hash
    cb.assert_called_once()


def test_process_item_worker_isdir(tmp_path):
    test_dir = tmp_path / "my_dir"
    test_dir.mkdir()

    cb = mock.MagicMock()
    item, text, h = process_item_worker(str(tmp_path), "my_dir", cb, db)
    assert text == "my_dir"
    assert h == ""
    cb.assert_called_once()


def test_process_item_worker_exception(tmp_path):
    cb = mock.MagicMock()
    with mock.patch(
        "app.core.scanner.parse_item_identifier", side_effect=Exception("Boom")
    ):
        item, text, h = process_item_worker(str(tmp_path), "test.txt", cb, db)
        assert item == "test.txt"
        assert text == ""
        assert h == ""
    cb.assert_called_once()


def test_build_corpus_generator_sequential_continue(tmp_path):
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello")
    file_hash = get_file_hash(str(file_path))
    db.upsert_document(str(tmp_path), "test.txt", file_hash, "hello")

    gen = build_corpus_generator(
        str(tmp_path),
        ["test.txt"],
        mock.MagicMock(),
        1,
        db=db,
        chunk_size=1,
        sequential=True,
    )
    chunks = list(gen)
    assert len(chunks) == 1
    assert "test.txt" in chunks[0]
    assert chunks[0]["test.txt"]["hash"] == file_hash

    gen2 = build_corpus_generator(
        str(tmp_path),
        ["test.txt"],
        mock.MagicMock(),
        1,
        db=db,
        chunk_size=2,
        sequential=True,
    )
    chunks2 = list(gen2)
    assert len(chunks2) == 1
    assert "test.txt" in chunks2[0]
    assert chunks2[0]["test.txt"]["hash"] == file_hash


def test_build_corpus_generator_parallel_continue(tmp_path):
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello")
    file_hash = get_file_hash(str(file_path))
    db.upsert_document(str(tmp_path), "test.txt", file_hash, "hello")

    gen = build_corpus_generator(
        str(tmp_path),
        ["test.txt"],
        mock.MagicMock(),
        1,
        db=db,
        chunk_size=1,
        sequential=False,
    )
    chunks = list(gen)
    assert len(chunks) == 1
    assert "test.txt" in chunks[0]
    assert chunks[0]["test.txt"]["hash"] == file_hash

    gen2 = build_corpus_generator(
        str(tmp_path),
        ["test.txt"],
        mock.MagicMock(),
        1,
        db=db,
        chunk_size=2,
        sequential=False,
    )
    chunks2 = list(gen2)
    assert len(chunks2) == 1
    assert "test.txt" in chunks2[0]
    assert chunks2[0]["test.txt"]["hash"] == file_hash


def test_mover_get_safe_path_samefile(tmp_path):
    f1 = tmp_path / "file.txt"
    f1.write_text("data")
    dest = tmp_path / "dest"
    dest.mkdir()
    f2 = dest / "file.txt"
    f2.write_text("data")

    with mock.patch(
        "app.core.mover.os.path.samefile", side_effect=OSError("Access Denied")
    ):
        safe = get_safe_path(str(dest), "file.txt", str(f1))
        assert safe != str(f2)
        assert safe == str(dest / "file_1.txt")


def test_execute_moves_ignores_malformed_plan_nodes(tmp_path):
    keep = tmp_path / "keep.txt"
    keep.write_text("data")
    execute_moves(str(tmp_path), {"__type__": "directory"}, db, None)
    assert keep.read_text() == "data"


def test_execute_moves_without_target_filename_keeps_name(tmp_path):
    f1 = tmp_path / "test.txt"
    f1.write_text("data")
    plan = {
        "dest": {"test.txt": {"__type__": "file", "relative_source": "../test.txt"}}
    }
    execute_moves(str(tmp_path), plan, db, None)
    assert (tmp_path / "dest" / "test.txt").read_text() == "data"
    assert not f1.exists()


def test_execute_moves_moves_symlink_as_link(tmp_path):
    import sys

    if sys.platform == "win32":
        return
    f1 = tmp_path / "target.txt"
    f1.write_text("data")
    sym = tmp_path / "link.txt"
    os.symlink("target.txt", str(sym))

    plan = {
        "dest": {
            "link.txt": {
                "__type__": "file",
                "relative_source": "../link.txt",
                "target_filename": "link.txt",
            }
        }
    }
    execute_moves(str(tmp_path), plan, db, None)
    assert (tmp_path / "dest" / "link.txt").is_symlink()
    assert not os.path.lexists(sym)


def test_extract_file_text_empty_file(tmp_path):
    file_path = tmp_path / "empty.txt"
    file_path.write_text("")
    with mock.patch(
        "app.core.extractor_strategies.TxtExtractor.extract", return_value="   "
    ):
        text = extract_file_text(str(file_path))
        assert text == "[STATUS:EMPTY]"


def test_process_item_worker_missing_file(tmp_path):
    cb = mock.MagicMock()
    item, text, h = process_item_worker(str(tmp_path), "does_not_exist", cb, db)
    assert text == ""
    assert h == ""


def test_mover_get_safe_path_no_source(tmp_path):
    f1 = tmp_path / "file.txt"
    f1.write_text("data")
    dest = tmp_path / "dest"
    dest.mkdir()
    f2 = dest / "file.txt"
    f2.write_text("data")

    # Missing coverage 27->36 (source_path is None)
    safe = get_safe_path(str(dest), "file.txt", None)
    assert safe == str(dest / "file_1.txt")


def test_mover_get_safe_path_source_missing(tmp_path):
    f1 = tmp_path / "file.txt"
    f1.write_text("data")
    dest = tmp_path / "dest"
    dest.mkdir()
    f2 = dest / "file.txt"
    f2.write_text("data")

    # source exists check fails
    safe = get_safe_path(str(dest), "file.txt", str(tmp_path / "missing.txt"))
    assert safe == str(dest / "file_1.txt")


def test_mover_remove_empty_dirs_not_dir(tmp_path):
    f1 = tmp_path / "file.txt"
    f1.write_text("data")
    _remove_empty_dirs(str(f1))
    assert f1.read_text() == "data"


def test_mover_remove_empty_dirs_with_files(tmp_path):
    d1 = tmp_path / "dir"
    d1.mkdir()
    f1 = d1 / "file.txt"
    f1.write_text("data")
    _remove_empty_dirs(str(d1))
    assert d1.exists()


class _CleanupSettings:
    CLEANUP_EMPTY_FOLDERS = True
    PROTECTED_PATHS: list[str] = []


class _NoCleanupSettings:
    CLEANUP_EMPTY_FOLDERS = False


def _delete_node(path, **extra):
    return {
        "__type__": "directory",
        "source_path": str(path),
        "status": "To Be Deleted",
        **extra,
    }


def test_execute_moves_deletes_empty_folder_marked_for_deletion(tmp_path):
    d1 = tmp_path / "delme"
    d1.mkdir()

    summary = execute_moves(
        str(tmp_path),
        {"delme": _delete_node(d1, protected=False)},
        db,
        history_manager,
        _CleanupSettings(),
    )

    assert not d1.exists()
    assert summary["deleted_folders"] == 1
    assert summary["protected_folders"] == 0


def test_execute_moves_cleanup_disabled(tmp_path):
    d1 = tmp_path / "delme"
    d1.mkdir()

    summary = execute_moves(
        str(tmp_path),
        {"delme": _delete_node(d1, protected=False)},
        db,
        history_manager,
        _NoCleanupSettings(),
    )

    assert d1.is_dir()
    assert summary["deleted_folders"] == 0
    assert summary["protected_folders"] == 1


def test_execute_moves_protected_folder(tmp_path):
    d1 = tmp_path / "delme"
    d1.mkdir()

    summary = execute_moves(
        str(tmp_path),
        {"delme": _delete_node(d1, protected=True)},
        db,
        history_manager,
        _CleanupSettings(),
    )

    assert d1.is_dir()
    assert summary["deleted_folders"] == 0
    assert summary["protected_folders"] == 1


def test_execute_moves_protected_paths_setting_keeps_folder(tmp_path):
    d1 = tmp_path / "delme"
    d1.mkdir()

    class Settings(_CleanupSettings):
        PROTECTED_PATHS = [str(d1)]

    summary = execute_moves(
        str(tmp_path),
        {"delme": _delete_node(d1, protected=False)},
        db,
        history_manager,
        Settings(),
    )

    assert d1.is_dir()
    assert summary["deleted_folders"] == 0
    assert summary["protected_folders"] == 1


def test_execute_moves_keeps_folder_marked_for_deletion_that_has_files(tmp_path):
    d1 = tmp_path / "delme"
    d1.mkdir()
    (d1 / "late.txt").write_text("arrived after planning")

    summary = execute_moves(
        str(tmp_path),
        {"delme": _delete_node(d1)},
        db,
        history_manager,
        _CleanupSettings(),
    )

    assert (d1 / "late.txt").read_text() == "arrived after planning"
    assert summary["deleted_folders"] == 0


def test_mover_optional_pylnk3_import_falls_back_to_none(tmp_path):
    # Run in a fresh interpreter so the import fallback is exercised without
    # reloading app.core.mover inside the shared test worker.
    import subprocess
    import sys

    code = (
        "import sys; sys.modules['pylnk3'] = None; "
        "import app.core.mover as m; "
        "assert m.pylnk3 is None"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr


def test_mover_remove_empty_dirs_recursive(tmp_path):
    d1 = tmp_path / "dir1"
    d1.mkdir()
    d2 = d1 / "dir2"
    d2.mkdir()
    _remove_empty_dirs(str(d1))
    assert not d1.exists()


def test_execute_moves_renames_tracked_document(tmp_path):
    f1 = tmp_path / "test.txt"
    f1.write_text("data")
    plan = {
        "dest": {
            "test.txt": {
                "__type__": "file",
                "relative_source": "../test.txt",
                "target_filename": "test2.txt",
                "user_confirmed": True,
            }
        }
    }
    db.upsert_document(str(tmp_path), "test.txt", "fake_hash", "data")

    execute_moves(str(tmp_path), plan, db, None)
    assert (tmp_path / "dest" / "test2.txt").read_text() == "data"


def test_execute_moves_loop_skip(tmp_path):
    def make_plan():
        d1 = tmp_path / "dir1"
        d2 = tmp_path / "dir2"
        d1.mkdir(exist_ok=True)
        d2.mkdir(exist_ok=True)
        plan = {
            "dir1": {
                "__type__": "directory",
                "source_path": str(d1),
                "status": "Skipped",
                "protected": False,
            },
            "dir2": _delete_node(d2, protected=False),
        }
        return plan, d1, d2

    plan, d1, d2 = make_plan()
    with mock.patch("app.core.verifier.VerificationEngine.get_moves", return_value=[]):
        summary = execute_moves(
            str(tmp_path), plan, db, history_manager, _CleanupSettings()
        )
    # Only the folder marked for deletion goes; the skipped one stays.
    assert d1.is_dir()
    assert not d2.exists()
    assert summary["deleted_folders"] == 1

    plan, d1, d2 = make_plan()
    with mock.patch("app.core.verifier.VerificationEngine.get_moves", return_value=[]):
        summary = execute_moves(
            str(tmp_path), plan, db, history_manager, _NoCleanupSettings()
        )
    assert d1.is_dir()
    assert d2.is_dir()
    assert summary["deleted_folders"] == 0


def test_execute_moves_find_dir_nodes_list(tmp_path):
    keep = tmp_path / "keep"
    keep.mkdir()
    plan = {"__type__": "directory", "nested": [{"status": "Moved"}]}

    with mock.patch("app.core.verifier.VerificationEngine.get_moves", return_value=[]):
        summary = execute_moves(
            str(tmp_path), plan, db, history_manager, _NoCleanupSettings()
        )

    assert not summary["cancelled"]
    assert summary["deleted_folders"] == 0
    assert keep.is_dir()


def test_remove_empty_dirs_exception(tmp_path):
    d1 = tmp_path / "delme"
    d1.mkdir()

    with mock.patch("os.rmdir", side_effect=OSError("Busy")):
        summary = execute_moves(
            str(tmp_path),
            {"delme": _delete_node(d1)},
            db,
            history_manager,
            _CleanupSettings(),
        )

    # A folder that can't be removed is left in place and not counted.
    assert d1.is_dir()
    assert summary["deleted_folders"] == 0
