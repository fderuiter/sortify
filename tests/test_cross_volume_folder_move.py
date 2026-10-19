"""Regression tests for #868: a cross-drive folder move must never delete the only copy."""

import errno
import os

import pytest

import app.core.resilient_file_ops as rfo
from app.core.exceptions import PartialMoveError


@pytest.fixture
def cross_drive(tmp_path, monkeypatch):
    """Make every rename of a user path fail with EXDEV, as it does across drives."""
    real_rename = rfo._rename_noreplace

    def rename_across_drives(src, dst):
        if not os.path.basename(src).startswith(".tmp_stage_"):
            raise OSError(errno.EXDEV, "Invalid cross-device link", src)
        return real_rename(src, dst)

    monkeypatch.setattr(rfo, "_rename_noreplace", rename_across_drives)
    monkeypatch.setattr(rfo, "RETRY_DELAY", 0)
    src = tmp_path / "drive_c" / "Project"
    (src / "sub").mkdir(parents=True)
    (tmp_path / "drive_d").mkdir()
    for rel in ("a.txt", "locked.txt", "sub/b.txt"):
        (src / rel).write_text(rel)
    return src, tmp_path / "drive_d" / "Project"


def _files(root):
    return sorted(
        os.path.relpath(os.path.join(d, n), root).replace(os.sep, "/")
        for d, _dirs, names in os.walk(root)
        for n in names
    )


def test_cross_drive_folder_move_moves_every_file(cross_drive):
    src, dst = cross_drive

    assert rfo.resilient_move(str(src), str(dst)) == str(dst)

    assert not src.exists()
    assert _files(dst) == ["a.txt", "locked.txt", "sub/b.txt"]


def test_partly_removed_source_folder_keeps_the_moved_copy(cross_drive, monkeypatch):
    """rmtree deletes some files, then hits one that another program has open."""
    src, dst = cross_drive
    real_rmtree = rfo.resilient_rmtree

    def rmtree_blocked_by_locked_file(path, ignore_errors=False):
        if os.path.normpath(path) == os.path.normpath(src):
            os.remove(src / "a.txt")
            os.remove(src / "sub" / "b.txt")
            raise PermissionError(errno.EACCES, "file in use", str(src / "locked.txt"))
        return real_rmtree(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(rfo, "resilient_rmtree", rmtree_blocked_by_locked_file)

    with pytest.raises(
        PartialMoveError, match="part of the source could not be removed"
    ):
        rfo.resilient_move(str(src), str(dst))

    assert _files(dst) == ["a.txt", "locked.txt", "sub/b.txt"]
    assert _files(src) == ["locked.txt"]
    assert not (dst.parent / "Project_1").exists(), "the move was retried"


def test_failed_source_file_removal_leaves_one_whole_copy(tmp_path, monkeypatch):
    """A single file is removed whole or not at all, so the copy can be dropped."""
    real_rename = rfo._rename_noreplace

    def rename_across_drives(src, dst):
        if not os.path.basename(src).startswith(".tmp_stage_"):
            raise OSError(errno.EXDEV, "Invalid cross-device link", src)
        return real_rename(src, dst)

    real_remove = rfo.resilient_remove

    def locked_remove(path):
        if os.path.normpath(path) == os.path.normpath(src):
            raise PermissionError(errno.EACCES, "file in use", path)
        return real_remove(path)

    monkeypatch.setattr(rfo, "_rename_noreplace", rename_across_drives)
    monkeypatch.setattr(rfo, "resilient_remove", locked_remove)
    src = tmp_path / "drive_c" / "report.txt"
    src.parent.mkdir()
    src.write_text("report")
    dst = tmp_path / "drive_d" / "report.txt"
    dst.parent.mkdir()

    with pytest.raises(PermissionError):
        rfo._cross_volume_atomic_move(str(src), str(dst))

    assert src.read_text() == "report"
    assert os.listdir(dst.parent) == []
