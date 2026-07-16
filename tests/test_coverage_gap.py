import hashlib
import os
import tempfile
from pathlib import Path
from unittest import mock

import numpy as np
import pypdf

from app.core.cache import CacheManager
from app.core.db import Database
from app.core.extractor import (
    build_corpus_generator,
    extract_file_text,
    get_file_hash,
    process_item_worker,
)
from app.core.history import HistoryManager
from app.core.mover import (
    _execute_moves_recursive,
    _remove_empty_dirs,
    execute_moves,
    get_safe_path,
)

_test_dir = tempfile.mkdtemp()
db = Database(Path(_test_dir) / "test.db")
cache_manager = CacheManager(str(Path(_test_dir) / "cache.db"))
history_manager = HistoryManager(db, cache_manager, str(Path(_test_dir) / "history.db"))
def save_cache_sync(*args, **kwargs):
    cache_manager.save_cache_sync(*args, **kwargs)


def test_get_file_hash_exception(tmp_path):
    with mock.patch("builtins.open", side_effect=PermissionError):
        h = get_file_hash(str(tmp_path / "nonexistent.txt"))
        assert h == hashlib.sha256().hexdigest()

def test_extract_file_text_status_empty(tmp_path):
    file_path = tmp_path / "dummy.txt"
    with open(file_path, "w") as f:
        f.write("Some data")
    
    with mock.patch("app.core.extractor_strategies.TxtExtractor.extract", return_value="   "):
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
    db.upsert_document(str(tmp_path), "test.txt", file_hash, "hello", np.array([0.1, 0.2]))
    
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
    with mock.patch("app.core.extractor.os.path.join", side_effect=Exception("Boom")):
        item, text, h = process_item_worker(str(tmp_path), "test.txt", cb, db)
        assert item == "test.txt"
        assert text == ""
        assert h == ""
    cb.assert_called_once()

def test_build_corpus_generator_sequential_continue(tmp_path):
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello")
    file_hash = get_file_hash(str(file_path))
    db.upsert_document(str(tmp_path), "test.txt", file_hash, "hello", np.array([0.1, 0.2]), model_name="test_model", vector_dimension=10)
    
    gen = build_corpus_generator(
        str(tmp_path), ["test.txt"], mock.MagicMock(), 1, chunk_size=1, sequential=True, active_model_name="test_model", active_dimension=10, db=db
    )
    chunks = list(gen)
    assert len(chunks) == 0

    gen2 = build_corpus_generator(
        str(tmp_path), ["test.txt"], mock.MagicMock(), 1, chunk_size=2, sequential=True, active_model_name="other_model", active_dimension=10, db=db
    )
    chunks2 = list(gen2)
    assert len(chunks2) == 1

def test_build_corpus_generator_parallel_continue(tmp_path):
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello")
    file_hash = get_file_hash(str(file_path))
    db.upsert_document(str(tmp_path), "test.txt", file_hash, "hello", np.array([0.1, 0.2]), model_name="test_model", vector_dimension=10)
    
    gen = build_corpus_generator(
        str(tmp_path), ["test.txt"], mock.MagicMock(), 1, chunk_size=1, sequential=False, active_model_name="test_model", active_dimension=10, db=db
    )
    chunks = list(gen)
    assert len(chunks) == 0

    gen2 = build_corpus_generator(
        str(tmp_path), ["test.txt"], mock.MagicMock(), 1, chunk_size=2, sequential=False, active_model_name="other_model", active_dimension=10, db=db
    )
    chunks2 = list(gen2)
    assert len(chunks2) == 1

def test_mover_get_safe_path_samefile(tmp_path):
    f1 = tmp_path / "file.txt"
    f1.write_text("data")
    dest = tmp_path / "dest"
    dest.mkdir()
    f2 = dest / "file.txt"
    f2.write_text("data")
    
    with mock.patch("os.path.samefile", side_effect=OSError("Access Denied")):
        safe = get_safe_path(str(dest), "file.txt", str(f1))
        assert safe != str(f2)
        assert safe == str(dest / "file_1.txt")

def test_execute_moves_recursive_dict_directory_and_invalid(tmp_path):
    plan = {
        "__type__": "directory"
    }
    _execute_moves_recursive(str(tmp_path), plan, db)
    
    plan2 = {
        "folder": {
            "__type__": "directory", "file": None
        }
    }
    _execute_moves_recursive(str(tmp_path), plan2, db)

def test_execute_moves_recursive_no_target_filename(tmp_path):
    f1 = tmp_path / "test.txt"
    f1.write_text("data")
    plan = {
        "test.txt": {
            "__type__": "file",
            "status": "Moved"
        }
    }
    _execute_moves_recursive(str(tmp_path), plan, db, "dest")
    assert (tmp_path / "dest" / "test.txt").exists()
    assert not f1.exists()

def test_execute_moves_recursive_symlink(tmp_path):
    import sys
    if sys.platform != "win32":
        f1 = tmp_path / "target.txt"
        f1.write_text("data")
        sym = tmp_path / "link.txt"
        os.symlink("target.txt", str(sym))
        
        plan = {
            "link.txt": {
                "__type__": "file",
                "status": "Moved",
                "target_filename": "link.txt"
            }
        }
        dest = tmp_path / "dest"
        _execute_moves_recursive(str(tmp_path), plan, db, "dest")
        assert (dest / "link.txt").is_symlink()
        assert not sym.exists()

def test_execute_moves_recursive_symlink_update_in_place(tmp_path):
    import sys
    if sys.platform != "win32":
        f1 = tmp_path / "target.txt"
        f1.write_text("data")
        sym = tmp_path / "link.txt"
        os.symlink("target.txt", str(sym))
        
        plan = {
            "link.txt": {
                "__type__": "file",
                "status": "Already Sorted",
                "target_filename": "link.txt"
            }
        }
        path_map = {os.path.abspath(f1): os.path.abspath(tmp_path / "new_target.txt")}
        _execute_moves_recursive(str(tmp_path), plan, db, "", path_map=path_map)
        assert sym.is_symlink()

def test_execute_moves_recursive_pylnk3(tmp_path):
    with mock.patch.dict("sys.modules", {"pylnk3": mock.MagicMock()}):
        import app.core.mover
        app.core.mover.pylnk3 = mock.MagicMock()
        
        f1 = tmp_path / "link.lnk"
        f1.write_text("lnkdata")
        
        with mock.patch("app.core.link_manager.LinkManager.get_link_info", return_value={"type": "lnk", "target": "target.txt"}):
            plan = {
                "link.lnk": {
                    "__type__": "file",
                    "status": "Moved",
                    "target_filename": "link.lnk"
                }
            }
            _execute_moves_recursive(str(tmp_path), plan, db, "dest")
            assert not f1.exists()

def test_execute_moves_recursive_pylnk3_exception(tmp_path):
    with mock.patch.dict("sys.modules", {"pylnk3": mock.MagicMock()}):
        import app.core.mover
        app.core.mover.pylnk3 = mock.MagicMock()
        app.core.mover.pylnk3.parse.side_effect = Exception("LNK Error")
        
        f1 = tmp_path / "link2.lnk"
        f1.write_text("lnkdata")
        
        with mock.patch("app.core.link_manager.LinkManager.get_link_info", return_value={"type": "lnk", "target": "target.txt"}):
            plan = {
                "link2.lnk": {
                    "__type__": "file",
                    "status": "Moved",
                    "target_filename": "link2.lnk"
                }
            }
            _execute_moves_recursive(str(tmp_path), plan, db, "dest")
            assert (tmp_path / "dest" / "link2.lnk").exists()

def test_execute_moves_recursive_rel_dest_key(tmp_path):
    f1 = tmp_path / "file.txt"
    f1.write_text("data")
    plan = {
        "file.txt": {
            "__type__": "file",
            "status": "Moved",
            "target_filename": "file.txt"
        }
    }
    _execute_moves_recursive(str(tmp_path), plan, db, "")
    assert f1.exists()

def test_extract_file_text_empty_file(tmp_path):
    file_path = tmp_path / "empty.txt"
    file_path.write_text("")
    with mock.patch("app.core.extractor_strategies.TxtExtractor.extract", return_value="   "):
        text = extract_file_text(str(file_path))
        assert text == "   "

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
    # coverage 44
    _remove_empty_dirs(str(f1))

def test_mover_remove_empty_dirs_with_files(tmp_path):
    d1 = tmp_path / "dir"
    d1.mkdir()
    f1 = d1 / "file.txt"
    f1.write_text("data")
    _remove_empty_dirs(str(d1))
    assert d1.exists()

def test_execute_moves_recursive_symlink_no_update(tmp_path):
    import sys
    if sys.platform != "win32":
        f1 = tmp_path / "target.txt"
        f1.write_text("data")
        sym = tmp_path / "link.txt"
        os.symlink("target.txt", str(sym))
        
        plan = {
            "link.txt": {
                "__type__": "file",
                "status": "Moved",
                "target_filename": "link.txt"
            }
        }
        with mock.patch("app.core.link_manager.LinkManager.get_link_info", return_value={"type": "symlink", "target": "target.txt"}):
            _execute_moves_recursive(str(tmp_path), plan, db, "")
        assert sym.is_symlink()

def test_execute_moves_cleanup_disabled(tmp_path):
    plan = {
        "delme": {
            "__type__": "directory",
            "source_path": "nonexistent",
            "status": "To Be Deleted",
            "protected": False
        }
    }
    class MockSettings:
        CLEANUP_EMPTY_FOLDERS = False
    
    execute_moves(str(tmp_path), plan, db, history_manager, MockSettings())

def test_execute_moves_protected_folder(tmp_path):
    plan = {
        "delme": {
            "__type__": "directory",
            "source_path": "nonexistent",
            "status": "To Be Deleted",
            "protected": True
        }
    }
    class MockSettings:
        CLEANUP_EMPTY_FOLDERS = True
    
    execute_moves(str(tmp_path), plan, db, history_manager, MockSettings())

def test_mover_import_error():
    with mock.patch.dict("sys.modules", {"pylnk3": None}):
        # reload app.core.mover to trigger import error
        import importlib

        import app.core.mover
        importlib.reload(app.core.mover)
        assert app.core.mover.pylnk3 is None
        
        # reload again to restore
        import sys
        if "pylnk3" in sys.modules:
            del sys.modules["pylnk3"]
        importlib.reload(app.core.mover)

def test_execute_moves_non_dict_plan(tmp_path):
    _execute_moves_recursive(str(tmp_path), "invalid_plan", db, "")

def test_mover_remove_empty_dirs_recursive(tmp_path):
    d1 = tmp_path / "dir1"
    d1.mkdir()
    d2 = d1 / "dir2"
    d2.mkdir()
    _remove_empty_dirs(str(d1))
    assert not d1.exists()

def test_execute_moves_recursive_symlink_needs_update(tmp_path):
    import sys
    if sys.platform != "win32":
        f1 = tmp_path / "target.txt"
        f1.write_text("data")
        sym = tmp_path / "link.txt"
        os.symlink(str(f1), str(sym))  # Absolute target
        
        plan = {
            "link.txt": {
                "__type__": "file",
                "status": "Moved",
                "target_filename": "link.txt"
            }
        }
        dest = tmp_path / "dest"
        dest.mkdir()
        # new_abs_target differs so it updates
        path_map = {os.path.abspath(f1): os.path.abspath(tmp_path / "new_target.txt")}
        with mock.patch("app.core.link_manager.LinkManager.get_link_info", return_value={"type": "symlink", "target": str(f1)}):
            _execute_moves_recursive(str(tmp_path), plan, db, "dest", path_map=path_map)

def test_execute_moves_recursive_db_doc_update(tmp_path):
    f1 = tmp_path / "test.txt"
    f1.write_text("data")
    plan = {
        "test.txt": {
            "__type__": "file",
            "status": "Moved",
            "target_filename": "test2.txt"
        }
    }
    
    file_hash = "fake_hash"
    db.upsert_document(str(tmp_path), "test.txt", file_hash, "data", np.array([0.1, 0.2]))
    
    _execute_moves_recursive(str(tmp_path), plan, db, "dest")
    assert (tmp_path / "dest" / "test2.txt").exists()

def test_execute_moves_recursive_symlink_needs_update_relative(tmp_path):
    import sys
    if sys.platform != "win32":
        f1 = tmp_path / "target.txt"
        f1.write_text("data")
        sym = tmp_path / "link.txt"
        os.symlink("target.txt", str(sym))  # Relative target
        
        plan = {
            "link.txt": {
                "__type__": "file",
                "status": "Moved",
                "target_filename": "link.txt"
            }
        }
        dest = tmp_path / "dest"
        dest.mkdir()
        # new_abs_target differs so it updates
        path_map = {os.path.abspath(f1): os.path.abspath(tmp_path / "new_target.txt")}
        with mock.patch("app.core.link_manager.LinkManager.get_link_info", return_value={"type": "symlink", "target": "target.txt"}):
            _execute_moves_recursive(str(tmp_path), plan, db, "", path_map=path_map)

def test_execute_moves_recursive_pylnk3_in_place(tmp_path):
    with mock.patch.dict("sys.modules", {"pylnk3": mock.MagicMock()}):
        import app.core.mover
        app.core.mover.pylnk3 = mock.MagicMock()
        
        f1 = tmp_path / "link.lnk"
        f1.write_text("lnkdata")
        
        with mock.patch("app.core.link_manager.LinkManager.get_link_info", return_value={"type": "lnk", "target": "target.txt"}):
            plan = {
                "link.lnk": {
                    "__type__": "file",
                    "status": "Moved",
                    "target_filename": "link.lnk"
                }
            }
            path_map = {os.path.abspath(tmp_path / "target.txt"): os.path.abspath(tmp_path / "new.txt")}
            _execute_moves_recursive(str(tmp_path), plan, db, "", path_map=path_map)

def test_execute_moves_loop_skip(tmp_path):
    plan = {
        "dir1": {
            "__type__": "directory",
            "source_path": "nonexistent",
            "status": "Skipped",
            "protected": False
        },
        "dir2": {
            "__type__": "directory",
            "source_path": "nonexistent",
            "status": "To Be Deleted",
            "protected": False
        }
    }
    class MockSettings:
        CLEANUP_EMPTY_FOLDERS = True
    
    with mock.patch("app.core.verifier.VerificationEngine.get_moves", return_value=[]):
        execute_moves(str(tmp_path), plan, db, history_manager, MockSettings())

    class MockSettingsFalse:
        CLEANUP_EMPTY_FOLDERS = False
    with mock.patch("app.core.verifier.VerificationEngine.get_moves", return_value=[]):
        execute_moves(str(tmp_path), plan, db, history_manager, MockSettingsFalse())

def test_execute_moves_find_dir_nodes_list(tmp_path):
    plan = {
        "__type__": "directory",
        "nested": [
            {"status": "Moved"}
        ]
    }
    class MockSettings:
        CLEANUP_EMPTY_FOLDERS = False
    
    with mock.patch("app.core.verifier.VerificationEngine.get_moves", return_value=[]):
        execute_moves(str(tmp_path), plan, db, history_manager, MockSettings())

def test_remove_empty_dirs_exception(tmp_path):
    d1 = tmp_path / "delme"
    d1.mkdir()
    plan = {
        "delme": {
            "__type__": "directory",
            "source_path": str(d1),
            "status": "To Be Deleted"
        }
    }
    class MockSettings:
        CLEANUP_EMPTY_FOLDERS = True

    with mock.patch("os.rmdir", side_effect=OSError("Busy")):
        try:
            execute_moves(str(tmp_path), plan, db, history_manager, MockSettings())
        except OSError:
            pass
