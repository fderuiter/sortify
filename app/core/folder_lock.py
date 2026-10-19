"""One sort at a time per folder (ADR 0007).

The lock is an OS advisory lock on a file under ``<app dir>/locks/``, named by
a hash of the folder path, so nothing is written into the user's folder. The
OS drops the lock when its holder exits or crashes, so there are no stale locks
to clean up. Two opens of the lock file conflict even inside one process, which
covers the TUI starting a second sort while one is still moving files (#734).
"""

import hashlib
import os
import sys
import time
from pathlib import Path
from typing import Optional


class FolderBusyError(RuntimeError):
    """Another sort is already moving files in this folder."""

    def __init__(self, folder: str):
        super().__init__(
            f"Another sort is already moving files in '{folder}'. "
            "Wait for it to finish, then try again."
        )
        self.folder = folder


def _lock_path(folder: str, lock_dir: Optional[Path]) -> Path:
    if lock_dir is None:
        from app.config import get_app_dir

        lock_dir = get_app_dir() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    key = os.path.normcase(os.path.realpath(os.path.abspath(folder)))
    return lock_dir / (hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".lock")


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class FolderLock:
    """Exclusive lock on one folder for the length of a ``with`` block.

    Waits up to ``timeout`` seconds for another holder to finish, then raises
    ``FolderBusyError``. A ``timeout`` of 0 fails at once.
    """

    def __init__(
        self, folder: str, timeout: float = 0.0, lock_dir: Optional[Path] = None
    ):
        self.folder = folder
        self.timeout = timeout
        self._path = _lock_path(folder, lock_dir)
        self._fd: Optional[int] = None

    def __enter__(self) -> "FolderLock":
        """Take the lock, waiting up to ``timeout`` seconds."""
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.timeout
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                os.close(fd)
                raise FolderBusyError(self.folder)
            time.sleep(0.1)
        self._fd = fd
        return self

    def __exit__(self, *exc) -> None:
        """Release the lock."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)
