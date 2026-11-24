"""One sort at a time per folder (ADR 0007).

The lock is an OS advisory lock on a file under ``<app dir>/locks/``, named by
a hash of the folder path, so nothing is written into the user's folder. The
OS drops the lock when its holder exits or crashes, so there are no stale locks
to clean up. Two opens of the lock file conflict even inside one process, which
covers the TUI starting a second sort while one is still moving files (#734).
"""

import contextlib
import hashlib
import os
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Optional


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


def canonical_folder_lock_key(folder: str) -> str:
    """Return canonical lock identity matching ``_lock_path`` hashing."""
    expanded = os.path.expanduser(str(folder).strip())
    return os.path.normcase(os.path.realpath(os.path.abspath(expanded)))


def lock_multiple_folders(
    folders: Iterable[str | None],
    *,
    timeout: float = 0.0,
    lock_dir: Optional[Path] = None,
) -> contextlib.ExitStack:
    """Acquire ``FolderLock``s across multiple roots in deterministic canonical order (ADR 0007).

    Deduplicates roots that resolve to the same canonical path so overlapping
    or symlinked roots never self-deadlock within the same caller.
    """
    by_canon_key: dict[str, str] = {}
    for raw_folder in folders:
        if not raw_folder or not str(raw_folder).strip():
            continue
        expanded = os.path.normpath(
            os.path.abspath(os.path.expanduser(str(raw_folder).strip()))
        )
        canon_key = canonical_folder_lock_key(expanded)
        by_canon_key.setdefault(canon_key, expanded)

    stack = contextlib.ExitStack()
    try:
        for _canon_key, folder_path in sorted(
            by_canon_key.items(), key=lambda kv: kv[0]
        ):
            stack.enter_context(
                FolderLock(folder_path, timeout=timeout, lock_dir=lock_dir)
            )
    except BaseException:
        stack.close()
        raise
    return stack


def collect_plan_external_roots(
    plan: Any,
    allowed_external_roots: Iterable[str] | None = None,
    *,
    settings: Any = None,
) -> list[str]:
    """Collect active external destination root folders referenced by ``plan``."""
    from app.core.path_utils import (
        _is_absolute_path,
        expand_target_path,
        is_external_subpath,
    )

    curr_plan = (
        plan.plan if hasattr(plan, "plan") and isinstance(plan.plan, dict) else plan
    )
    if not isinstance(curr_plan, dict):
        return []

    eff_roots = (
        allowed_external_roots
        if allowed_external_roots is not None
        else (
            getattr(settings, "ALLOWED_EXTERNAL_ROOTS", None)
            if settings is not None
            else None
        )
    )
    configured_roots = [
        expand_target_path(str(r).strip())
        for r in (eff_roots or ())
        if r and str(r).strip()
    ]

    found: list[str] = []

    def _record_external(raw_target: str) -> None:
        exp = expand_target_path(raw_target.strip())
        if not _is_absolute_path(exp):
            return
        matched_root: str | None = None
        for cfg_root in configured_roots:
            if is_external_subpath(exp, cfg_root):
                matched_root = cfg_root
                break
        chosen = matched_root or exp
        if chosen not in found:
            found.append(chosen)

    def _walk(node: dict[str, Any], depth: int = 0) -> None:
        for key, val in node.items():
            if depth == 0:
                _record_external(str(key))
            if isinstance(val, dict):
                if val.get("__type__") == "file":
                    override = val.get("destination_override")
                    if isinstance(override, str) and override.strip():
                        _record_external(override)
                elif val.get("__type__") != "directory":
                    _walk(val, depth + 1)

    _walk(curr_plan, 0)
    return found
