"""Every way a move can fail must leave the source whole and never replace a file.

These cover the fallback paths of ``_rename_noreplace`` (no native exclusive
rename, no hard links, folders) and the races in the cross-drive copy that the
happy-path tests in test_noreplace_move.py and test_atomic_non_clobber_move.py
don't reach. ADR 0005: moves never destroy unverified bytes.
"""

import errno
import os

import pytest

import app.core.resilient_file_ops as rfo

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="Windows renames with MoveFile, not these fallbacks"
)


@pytest.fixture
def no_native_noreplace(monkeypatch):
    """A filesystem whose kernel rejects RENAME_NOREPLACE, like some FUSE and SMB mounts."""
    monkeypatch.setattr(
        rfo, "_load_native_noreplace", lambda: lambda _src, _dst: errno.EINVAL
    )


@pytest.fixture
def cross_drive(monkeypatch):
    """Make renames of user paths fail with EXDEV, as they do across drives."""
    real_rename = rfo._rename_noreplace

    def rename_across_drives(src, dst):
        if not os.path.basename(src).startswith(".tmp_stage_"):
            raise OSError(errno.EXDEV, "Invalid cross-device link", src)
        return real_rename(src, dst)

    monkeypatch.setattr(rfo, "_rename_noreplace", rename_across_drives)
    monkeypatch.setattr(rfo, "RETRY_DELAY", 0)


def _stage_files(folder):
    return [n for n in os.listdir(folder) if n.startswith(".tmp_stage_")]


@posix_only
def test_unsupported_native_rename_falls_back_and_still_refuses_to_replace(
    tmp_path, no_native_noreplace
):
    src = tmp_path / "new.txt"
    dst = tmp_path / "existing.txt"
    src.write_text("new")
    dst.write_text("existing")

    with pytest.raises(FileExistsError):
        rfo._rename_noreplace(str(src), str(dst))

    assert src.read_text() == "new"
    assert dst.read_text() == "existing"


@posix_only
def test_unsupported_native_rename_falls_back_to_a_working_move(
    tmp_path, no_native_noreplace
):
    src = tmp_path / "new.txt"
    dst = tmp_path / "moved.txt"
    src.write_text("new")

    rfo._rename_noreplace(str(src), str(dst))

    assert not src.exists()
    assert dst.read_text() == "new"


@posix_only
def test_folder_fallback_refuses_an_existing_target(tmp_path, no_native_noreplace):
    src = tmp_path / "Inbox"
    dst = tmp_path / "Archive"
    src.mkdir()
    (src / "a.txt").write_text("a")
    dst.mkdir()
    (dst / "b.txt").write_text("b")

    with pytest.raises(FileExistsError):
        rfo._rename_noreplace(str(src), str(dst))

    assert (src / "a.txt").read_text() == "a"
    assert sorted(os.listdir(dst)) == ["b.txt"]


@posix_only
def test_folder_fallback_removes_its_reservation_when_the_rename_fails(
    tmp_path, no_native_noreplace, monkeypatch
):
    src = tmp_path / "Inbox"
    dst = tmp_path / "Archive"
    src.mkdir()
    (src / "a.txt").write_text("a")

    def rename_fails(_src, _dst):
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(os, "rename", rename_fails)

    with pytest.raises(PermissionError):
        rfo._rename_noreplace(str(src), str(dst))

    assert (src / "a.txt").read_text() == "a"
    assert not dst.exists(), "the empty reservation folder was left behind"


@posix_only
def test_hard_link_fallback_keeps_one_copy_when_the_source_cannot_be_unlinked(
    tmp_path, no_native_noreplace, monkeypatch
):
    src = tmp_path / "new.txt"
    dst = tmp_path / "moved.txt"
    src.write_text("new")
    real_unlink = os.unlink

    def source_in_use(path, *args, **kwargs):
        if os.path.normpath(path) == os.path.normpath(src):
            raise PermissionError(errno.EBUSY, "in use", path)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", source_in_use)

    with pytest.raises(PermissionError):
        rfo._rename_noreplace(str(src), str(dst))

    assert src.read_text() == "new"
    assert not dst.exists()


def test_cross_drive_move_never_replaces_a_file_that_appears_during_the_copy(
    tmp_path, cross_drive, monkeypatch
):
    """Another program saves a file at the target while Sortify is copying."""
    src = tmp_path / "drive_c" / "report.txt"
    dst = tmp_path / "drive_d" / "report.txt"
    src.parent.mkdir()
    dst.parent.mkdir()
    src.write_text("sortify's copy")
    real_fsync = rfo._fsync_path

    def other_program_saves(path):
        if os.path.basename(path).startswith(".tmp_stage_") and not dst.exists():
            dst.write_text("the other program's file")
        return real_fsync(path)

    monkeypatch.setattr(rfo, "_fsync_path", other_program_saves)

    with pytest.raises(FileExistsError):
        rfo._cross_volume_atomic_move(str(src), str(dst))

    assert src.read_text() == "sortify's copy"
    assert dst.read_text() == "the other program's file"
    assert _stage_files(dst.parent) == []


def test_cross_drive_move_with_a_failed_copy_keeps_the_source(
    tmp_path, cross_drive, monkeypatch
):
    src = tmp_path / "drive_c" / "report.txt"
    dst = tmp_path / "drive_d" / "report.txt"
    src.parent.mkdir()
    dst.parent.mkdir()
    src.write_text("report")

    def disk_full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(rfo.shutil, "copy2", disk_full)

    with pytest.raises(OSError, match="No space left"):
        rfo._cross_volume_atomic_move(str(src), str(dst))

    assert src.read_text() == "report"
    assert os.listdir(dst.parent) == []


@pytest.mark.skipif(
    os.name == "nt", reason="creating symlinks needs admin rights on Windows"
)
def test_cross_drive_move_of_a_symlink_moves_the_link_not_its_target(
    tmp_path, cross_drive
):
    target = tmp_path / "drive_c" / "real.txt"
    target.parent.mkdir()
    target.write_text("real")
    link = tmp_path / "drive_c" / "shortcut.txt"
    os.symlink(target, link)
    dst = tmp_path / "drive_d" / "shortcut.txt"
    dst.parent.mkdir()

    rfo.resilient_move(str(link), str(dst))

    assert not os.path.lexists(link)
    assert os.path.islink(dst)
    assert os.readlink(dst) == str(target)
    assert target.read_text() == "real"


def test_moving_a_missing_source_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(rfo, "RETRY_DELAY", 0)
    dst = tmp_path / "Docs" / "report.txt"

    with pytest.raises(FileNotFoundError):
        rfo.resilient_move(str(tmp_path / "missing.txt"), str(dst))

    assert not dst.exists()
