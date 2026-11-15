"""Verify compatibility between discovery and recovery hash retry profiles."""

import hashlib
from unittest.mock import Mock

import pytest

from app.core import resilient_file_ops as ops


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)
    monkeypatch.setattr(ops.gc, "collect", lambda: None)


def test_missing_hash_keeps_legacy_compatibility():
    reader = Mock(side_effect=AssertionError("must not read"))
    assert ops.verify_file_hash("missing", None, hash_file=reader)
    reader.assert_not_called()


@pytest.mark.parametrize("windows", [True, False])
@pytest.mark.parametrize("recovery", [True, False])
def test_matching_hash_never_retries(windows, recovery):
    reader = Mock(return_value="expected")
    assert ops.verify_file_hash(
        "file", "expected", windows=windows, recovery=recovery, hash_file=reader
    )
    assert reader.call_count == 1


def test_posix_recovery_retries_transient_empty_known_nonempty_file():
    reader = Mock(side_effect=[hashlib.sha256(b"").hexdigest(), "expected"])
    assert ops.verify_file_hash("file", "expected", 12, windows=False, hash_file=reader)
    assert reader.call_count == 2


@pytest.mark.parametrize("recovery,size", [(False, 12), (True, 0)])
def test_posix_discovery_and_unknown_size_do_not_retry_empty_mismatch(recovery, size):
    reader = Mock(return_value=hashlib.sha256(b"").hexdigest())
    assert not ops.verify_file_hash(
        "file", "expected", size, windows=False, recovery=recovery, hash_file=reader
    )
    assert reader.call_count == 1


def test_posix_recovery_does_not_retry_nonempty_mismatch():
    reader = Mock(return_value="wrong")
    assert not ops.verify_file_hash(
        "file", "expected", 12, windows=False, hash_file=reader
    )
    assert reader.call_count == 1


@pytest.mark.parametrize("windows,attempts", [(False, 10), (True, 30)])
def test_recovery_exhaustion_never_verifies_unreadable_bytes(windows, attempts):
    reader = Mock(side_effect=PermissionError("locked"))
    assert not ops.verify_file_hash(
        "file", "expected", windows=windows, hash_file=reader
    )
    assert reader.call_count == attempts


def test_discovery_windows_uses_standard_retry_schedule(monkeypatch):
    monkeypatch.setattr(ops, "MAX_ATTEMPTS", 3)
    reader = Mock(side_effect=[OSError("locked"), "wrong", "expected"])
    assert ops.verify_file_hash(
        "file", "expected", recovery=False, windows=True, hash_file=reader
    )
    assert reader.call_count == 3


def test_actual_bytes_and_missing_file(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("history bytes")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert ops.verify_file_hash(path, digest)
    assert not ops.verify_file_hash(
        tmp_path / "missing", digest, windows=False, recovery=False
    )
