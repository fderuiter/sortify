"""Regression tests for #718: non-clobber moves must never replace an existing file."""

import errno
import os
import threading
from unittest.mock import patch

import pytest

import app.core.resilient_file_ops as rfo
from app.core.resilient_file_ops import (
    _cross_volume_atomic_move,
    atomic_move_non_clobber,
    resilient_move,
)


def _race_same_name_moves(tmp_path, trials=40, workers=4):
    lost = 0
    for trial in range(trials):
        root = tmp_path / f"t{trial}"
        cat = root / "Cat"
        cat.mkdir(parents=True)
        sources = []
        for i in range(workers):
            src_dir = root / f"s{i}"
            src_dir.mkdir()
            src = src_dir / "report.pdf"
            src.write_text(str(i))
            sources.append(str(src))

        barrier = threading.Barrier(workers)

        def run(path):
            barrier.wait()
            resilient_move(path, str(cat / "report.pdf"))

        threads = [threading.Thread(target=run, args=(p,)) for p in sources]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        survivors = sorted(p.read_text() for p in cat.iterdir())
        lost += workers - len(survivors)
    return lost


def test_concurrent_same_name_moves_lose_nothing(tmp_path):
    assert _race_same_name_moves(tmp_path) == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX link fallback")
def test_link_fallback_is_also_exclusive(tmp_path):
    with (
        patch.object(rfo, "_native_noreplace_loaded", True),
        patch.object(rfo, "_native_noreplace_fn", None),
    ):
        assert _race_same_name_moves(tmp_path) == 0


def test_permission_error_does_not_fall_back_to_copy(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("data")
    dst = tmp_path / "out" / "a.txt"

    with (
        patch.object(rfo, "_rename_noreplace", side_effect=PermissionError("denied")),
        patch.object(rfo, "_cross_volume_atomic_move") as cross_volume,
    ):
        with pytest.raises(PermissionError):
            atomic_move_non_clobber(str(src), str(dst))

    cross_volume.assert_not_called()
    assert src.read_text() == "data"


def test_cross_device_error_uses_copy_path(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("data")
    dst = tmp_path / "out" / "a.txt"
    real_rename = rfo._rename_noreplace

    def rename(s, d):
        if os.path.basename(s).startswith(".tmp_stage_"):
            return real_rename(s, d)
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    with patch.object(rfo, "_rename_noreplace", side_effect=rename):
        atomic_move_non_clobber(str(src), str(dst))

    assert dst.read_text() == "data"
    assert not src.exists()
    assert not [n for n in os.listdir(dst.parent) if n.startswith(".tmp_stage_")]


def test_cross_volume_content_mismatch_keeps_source(tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"original bytes")
    dst = tmp_path / "out" / "a.bin"
    dst.parent.mkdir()

    with patch.object(rfo, "_hash_file", side_effect=["aaa", "bbb"]):
        with pytest.raises(RuntimeError, match="does not match"):
            _cross_volume_atomic_move(str(src), str(dst))

    assert src.read_bytes() == b"original bytes"
    assert not dst.exists()
    assert not [n for n in os.listdir(dst.parent) if n.startswith(".tmp_stage_")]


@pytest.mark.skipif(os.name == "nt", reason="POSIX link fallback")
def test_filesystem_without_hard_links_still_refuses_to_replace(tmp_path):
    real_link = os.link

    def no_links(*args, **kwargs):
        raise OSError(errno.EPERM, "Operation not permitted")

    with (
        patch.object(rfo, "_native_noreplace_loaded", True),
        patch.object(rfo, "_native_noreplace_fn", None),
        patch("os.link", side_effect=no_links),
    ):
        assert _race_same_name_moves(tmp_path) == 0

        src = tmp_path / "x.txt"
        src.write_text("new")
        taken = tmp_path / "taken.txt"
        taken.write_text("old")
        with pytest.raises(FileExistsError):
            atomic_move_non_clobber(str(src), str(taken))
        assert taken.read_text() == "old"
    assert os.link is real_link
