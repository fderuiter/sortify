"""The pre-move hash is a SHA-256 of the file's bytes, not the stored DB hash (#844)."""

import hashlib
import os

import pytest

from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager
from app.core.mover import execute_moves


@pytest.fixture
def env(tmp_path):
    base_dir = tmp_path / "inbox"
    base_dir.mkdir()
    worker = DBWorker()
    db = Database(tmp_path / "docs.db", worker=worker)
    cache = CacheManager(str(tmp_path / "cache.db"), worker=worker)
    history = HistoryManager(db, cache, str(tmp_path / "history.db"))
    yield str(base_dir), db, history
    worker.stop()
    from app.core.db_conn import clear_connection_cache

    clear_connection_cache()


def test_text_md5_in_db_does_not_fail_verification(env):
    base_dir, db, history = env
    text = "# Notes\n\nQuarterly planning notes."
    with open(os.path.join(base_dir, "notes.md"), "w", encoding="utf-8") as f:
        f.write(text)
    # What the analyzer stores when it has no file hash: an MD5 of the text.
    db.upsert_document(
        base_dir, "notes.md", hashlib.md5(text.encode("utf-8")).hexdigest(), text
    )

    plan = {
        "Notes": {
            "notes.md": {
                "__type__": "file",
                "relative_source": "../notes.md",
                "target_filename": "notes.md",
            }
        }
    }
    summary = execute_moves(base_dir, plan, db, history)

    records = summary["audit_report"]["records"]
    assert [r["status"] for r in records] == ["VERIFIED_PASS"]


def test_same_filesystem_move_skips_dest_hash_read(env, monkeypatch):
    base_dir, db, history = env
    src_file = os.path.join(base_dir, "report.pdf")
    with open(src_file, "wb") as f:
        f.write(b"%PDF-1.4 sample content for hash test")

    import app.core.extractor as extractor

    original_get_file_hash = extractor.get_file_hash
    calls = []

    def tracked_get_file_hash(path):
        calls.append(os.path.abspath(path))
        return original_get_file_hash(path)

    monkeypatch.setattr(extractor, "get_file_hash", tracked_get_file_hash)

    plan = {
        "Reports": {
            "report.pdf": {
                "__type__": "file",
                "relative_source": "../report.pdf",
                "target_filename": "report.pdf",
            }
        }
    }
    summary = execute_moves(base_dir, plan, db, history)

    dest_file = os.path.join(base_dir, "Reports", "report.pdf")
    # Source file was hashed before the move, but destination file was NOT re-read
    assert os.path.abspath(src_file) in calls
    assert os.path.abspath(dest_file) not in calls

    records = summary["audit_report"]["records"]
    assert [r["status"] for r in records] == ["VERIFIED_PASS"]


def test_zero_or_mismatched_inode_falls_back_to_dest_hash(env, monkeypatch):
    base_dir, db, history = env
    src_file = os.path.join(base_dir, "doc.txt")
    with open(src_file, "w", encoding="utf-8") as f:
        f.write("Test content for fallback")

    import app.core.extractor as extractor

    original_get_file_hash = extractor.get_file_hash
    calls = []

    def tracked_get_file_hash(path):
        calls.append(os.path.abspath(path))
        return original_get_file_hash(path)

    monkeypatch.setattr(extractor, "get_file_hash", tracked_get_file_hash)

    real_stat = os.stat

    class FakeStatResult:
        def __init__(self, orig, **overrides):
            self._orig = orig
            self._overrides = overrides

        def __getattr__(self, name):
            if name in self._overrides:
                return self._overrides[name]
            return getattr(self._orig, name)

    def fake_stat(path, *args, **kwargs):
        res = real_stat(path, *args, **kwargs)
        if "Docs" in str(path):
            # Destination has inode 0 (unsupported filesystem / FAT32)
            return FakeStatResult(res, st_ino=0)
        return res

    monkeypatch.setattr(os, "stat", fake_stat)

    plan = {
        "Docs": {
            "doc.txt": {
                "__type__": "file",
                "relative_source": "../doc.txt",
                "target_filename": "doc.txt",
            }
        }
    }
    summary = execute_moves(base_dir, plan, db, history)

    dest_file = os.path.join(base_dir, "Docs", "doc.txt")
    assert os.path.abspath(src_file) in calls
    # Because inode was zero, destination file was re-read via fallback
    assert os.path.abspath(dest_file) in calls

    records = summary["audit_report"]["records"]
    assert [r["status"] for r in records] == ["VERIFIED_PASS"]


def test_different_inode_falls_back_to_dest_hash(env, monkeypatch):
    base_dir, db, history = env
    src_file = os.path.join(base_dir, "different_ino.txt")
    with open(src_file, "w", encoding="utf-8") as f:
        f.write("Test content for inode mismatch")

    import app.core.extractor as extractor

    original_get_file_hash = extractor.get_file_hash
    calls = []

    def tracked_get_file_hash(path):
        calls.append(os.path.abspath(path))
        return original_get_file_hash(path)

    monkeypatch.setattr(extractor, "get_file_hash", tracked_get_file_hash)

    real_stat = os.stat

    class FakeStatResult:
        def __init__(self, orig, **overrides):
            self._orig = orig
            self._overrides = overrides

        def __getattr__(self, name):
            if name in self._overrides:
                return self._overrides[name]
            return getattr(self._orig, name)

    def fake_stat(path, *args, **kwargs):
        res = real_stat(path, *args, **kwargs)
        if "Different" in str(path):
            # Destination has a different inode number
            return FakeStatResult(res, st_ino=res.st_ino + 12345)
        return res

    monkeypatch.setattr(os, "stat", fake_stat)

    plan = {
        "Different": {
            "different_ino.txt": {
                "__type__": "file",
                "relative_source": "../different_ino.txt",
                "target_filename": "different_ino.txt",
            }
        }
    }
    summary = execute_moves(base_dir, plan, db, history)

    dest_file = os.path.join(base_dir, "Different", "different_ino.txt")
    assert os.path.abspath(dest_file) in calls

    records = summary["audit_report"]["records"]
    assert [r["status"] for r in records] == ["VERIFIED_PASS"]


def test_device_or_size_mismatch_falls_back_to_dest_hash(env, monkeypatch):
    base_dir, db, history = env
    src_file = os.path.join(base_dir, "device_test.txt")
    with open(src_file, "w", encoding="utf-8") as f:
        f.write("Testing cross-device / size mismatch fallback")

    import app.core.extractor as extractor

    original_get_file_hash = extractor.get_file_hash
    calls = []

    def tracked_get_file_hash(path):
        calls.append(os.path.abspath(path))
        return original_get_file_hash(path)

    monkeypatch.setattr(extractor, "get_file_hash", tracked_get_file_hash)

    real_stat = os.stat

    class FakeStatResult:
        def __init__(self, orig, **overrides):
            self._orig = orig
            self._overrides = overrides

        def __getattr__(self, name):
            if name in self._overrides:
                return self._overrides[name]
            return getattr(self._orig, name)

    def fake_stat(path, *args, **kwargs):
        res = real_stat(path, *args, **kwargs)
        if "DeviceCross" in str(path):
            # Cross-device move simulation: different st_dev
            return FakeStatResult(res, st_dev=res.st_dev + 1)
        return res

    monkeypatch.setattr(os, "stat", fake_stat)

    plan = {
        "DeviceCross": {
            "device_test.txt": {
                "__type__": "file",
                "relative_source": "../device_test.txt",
                "target_filename": "device_test.txt",
            }
        }
    }
    summary = execute_moves(base_dir, plan, db, history)

    dest_file = os.path.join(base_dir, "DeviceCross", "device_test.txt")
    assert os.path.abspath(dest_file) in calls

    records = summary["audit_report"]["records"]
    assert [r["status"] for r in records] == ["VERIFIED_PASS"]
