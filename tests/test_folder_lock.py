"""Tests for the per-folder sort lock (ADR 0007)."""

import multiprocessing
import time

import pytest

from app.core.folder_lock import FolderBusyError, FolderLock


def test_second_holder_in_the_same_process_is_refused(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    locks = tmp_path / "locks"

    with FolderLock(str(folder), lock_dir=locks):
        with pytest.raises(FolderBusyError, match="already moving files"):
            with FolderLock(str(folder), lock_dir=locks):
                pass


def test_lock_is_free_again_after_release(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    locks = tmp_path / "locks"

    with FolderLock(str(folder), lock_dir=locks):
        pass
    # Taking the lock again raises FolderBusyError if release leaked it, and
    # the new holder really holds it.
    with FolderLock(str(folder), lock_dir=locks):
        with pytest.raises(FolderBusyError):
            with FolderLock(str(folder), lock_dir=locks):
                pass


def test_different_folders_do_not_block_each_other(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    locks = tmp_path / "locks"

    with FolderLock(str(tmp_path / "a"), lock_dir=locks):
        with FolderLock(str(tmp_path / "b"), lock_dir=locks):
            # Both are held at once, each still refusing its own second holder.
            for name in ("a", "b"):
                with pytest.raises(FolderBusyError):
                    with FolderLock(str(tmp_path / name), lock_dir=locks):
                        pass


def test_same_folder_spelled_differently_is_one_lock(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    locks = tmp_path / "locks"

    with FolderLock(str(folder), lock_dir=locks):
        with pytest.raises(FolderBusyError):
            with FolderLock(str(folder) + "/./", lock_dir=locks):
                pass


def test_waiter_gets_the_lock_when_the_holder_releases_in_time(tmp_path):
    import threading

    folder = tmp_path / "docs"
    folder.mkdir()
    locks = tmp_path / "locks"
    holder = FolderLock(str(folder), lock_dir=locks)
    holder.__enter__()
    threading.Timer(0.3, holder.__exit__).start()

    start = time.monotonic()
    with FolderLock(str(folder), timeout=5.0, lock_dir=locks):
        pass
    assert time.monotonic() - start < 5.0


def _hold(folder, locks, ready, release):
    with FolderLock(folder, lock_dir=locks):
        ready.set()
        release.wait(10)


def test_lock_held_by_another_process_is_refused(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    locks = tmp_path / "locks"
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold, args=(str(folder), locks, ready, release))
    proc.start()
    try:
        assert ready.wait(30)
        with pytest.raises(FolderBusyError):
            with FolderLock(str(folder), lock_dir=locks):
                pass
    finally:
        release.set()
        proc.join(30)

    with FolderLock(str(folder), lock_dir=locks):
        pass


def test_session_refuses_to_move_while_another_sort_holds_the_folder(tmp_path):
    from app.config import AppSettings
    from app.core.session import AppSession

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("finance report")
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    session = AppSession(settings, base_dir=str(src))
    session.folder_lock_timeout = 0.0
    try:
        with FolderLock(str(src)):
            with pytest.raises(FolderBusyError):
                session.execute_moves({})
    finally:
        session.close()
    assert (src / "a.txt").exists()
