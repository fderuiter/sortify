"""Unified Resilient File Operations Module.

Provides a centralized, single standardized retry timing and count profile on Windows
for moving, deleting, hashing, and cleaning up directories.
"""

import ctypes
import errno
import gc
import hashlib
import logging
import os
import shutil
import stat
import sys
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from typing import Optional

from app.core.exceptions import PartialMoveError
from app.core.path_utils import is_junction_path

# Centralized Retry Engine configuration
IS_WINDOWS = sys.platform == "win32"

# Single standardized retry timing and count profile on Windows:
# 15 attempts with 0.05 seconds sleep delay.
# macOS and Linux bypass retry cycles entirely (1 attempt, 0.0s delay).
MAX_ATTEMPTS = 15 if IS_WINDOWS else 1
RETRY_DELAY = 0.05 if IS_WINDOWS else 0.0


def verify_file_hash(
    file_path: str | os.PathLike,
    expected_hash: str | None,
    expected_size: int = 0,
    *,
    recovery: bool = True,
    windows: bool | None = None,
    hash_file: Callable | None = None,
) -> bool:
    """Verify legacy history hashes using the caller's existing retry policy.

    Discovery uses the standard file-operation retry schedule and retries only
    on Windows. Recovery additionally retries transient empty reads of a known
    nonempty file and reader failures on other platforms. Missing expected
    hashes retain legacy compatibility; a failed read never verifies a hash.
    """
    if not expected_hash:
        return True
    if hash_file is None:
        from app.core.extractor import get_file_hash

        hash_file = get_file_hash
    windows = sys.platform == "win32" if windows is None else windows
    attempts = (30 if windows else 10) if recovery else MAX_ATTEMPTS
    delay = (0.1 if windows else 0.05) if recovery else RETRY_DELAY
    empty_hash = hashlib.sha256(b"").hexdigest()
    for attempt in range(attempts):
        try:
            actual = hash_file(os.fspath(file_path))
            if actual == expected_hash:
                return True
            retry_empty = (
                recovery
                and actual == empty_hash
                and expected_hash != empty_hash
                and expected_size > 0
            )
            if not windows and not retry_empty:
                return False
        except Exception as error:
            logging.debug("Hash verification read failed (%s)", type(error).__name__)
            if not recovery and not windows:
                return False
        if not recovery and attempt == attempts - 1:
            break
        gc.collect()
        if delay > 0:
            time.sleep(delay)
    return False


def _is_same_path(p1: str, p2: str) -> bool:
    if p1 is None or p2 is None:
        return p1 == p2
    try:
        if os.path.lexists(p1) and os.path.lexists(p2):
            return os.path.samefile(p1, p2)
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(p1)) == os.path.normcase(
        os.path.abspath(p2)
    )


_RENAME_NOREPLACE_LINUX = 1
_RENAME_EXCL_DARWIN = 0x4
_AT_FDCWD = -100
_native_noreplace_fn = None
_native_noreplace_loaded = False
# Serialises the last-resort check-then-rename within this process.
_noreplace_fallback_lock = threading.Lock()
_LINK_UNSUPPORTED = {
    errno.EPERM,
    errno.EMLINK,
    getattr(errno, "ENOTSUP", errno.EPERM),
    getattr(errno, "EOPNOTSUPP", errno.EPERM),
}
_NOREPLACE_UNSUPPORTED = {
    errno.EINVAL,
    errno.ENOSYS,
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
}


def _load_native_noreplace():
    """Return a callable ``(src_bytes, dst_bytes) -> errno`` for an exclusive rename, or None."""
    global _native_noreplace_fn, _native_noreplace_loaded
    if _native_noreplace_loaded:
        return _native_noreplace_fn
    _native_noreplace_loaded = True
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
            fn = libc.renameat2
            fn.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            fn.restype = ctypes.c_int

            def _call(src: bytes, dst: bytes) -> int:
                if fn(_AT_FDCWD, src, _AT_FDCWD, dst, _RENAME_NOREPLACE_LINUX) == 0:
                    return 0
                return ctypes.get_errno()

            _native_noreplace_fn = _call
        elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
            fn = libc.renamex_np
            fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            fn.restype = ctypes.c_int

            def _call(src: bytes, dst: bytes) -> int:
                if fn(src, dst, _RENAME_EXCL_DARWIN) == 0:
                    return 0
                return ctypes.get_errno()

            _native_noreplace_fn = _call
    except Exception:
        _native_noreplace_fn = None
    return _native_noreplace_fn


def _rename_noreplace(src: str, dst: str) -> None:
    """Rename ``src`` to ``dst`` atomically, never replacing an existing ``dst``.

    Raises FileExistsError if ``dst`` exists, and OSError with ``errno.EXDEV``
    when the paths are on different filesystems.
    """
    if IS_WINDOWS:
        # Windows MoveFile semantics already refuse to replace an existing target.
        os.rename(src, dst)
        return

    native = _load_native_noreplace()
    if native is not None:
        err = native(os.fsencode(src), os.fsencode(dst))
        if err == 0:
            return
        if err == errno.EEXIST:
            raise FileExistsError(err, os.strerror(err), dst)
        if err not in _NOREPLACE_UNSUPPORTED:
            raise OSError(err, os.strerror(err), src, None, dst)
        # The filesystem does not support exclusive rename; fall back below.

    if os.path.isdir(src) and not os.path.islink(src):
        # Directories cannot be hard-linked. Reserve the name with an exclusive
        # mkdir, then rename over the empty reservation.
        os.mkdir(dst)
        try:
            os.rename(src, dst)
        except OSError:
            try:
                os.rmdir(dst)
            except OSError:
                pass
            raise
        return

    # link() fails with EEXIST if dst exists and EXDEV across filesystems.
    try:
        os.link(src, dst, follow_symlinks=False)
    except OSError as err:
        if err.errno not in _LINK_UNSUPPORTED:
            raise
        # No hard links either (e.g. FAT/exFAT): check-then-rename, serialised
        # so this process's own workers can't race each other.
        with _noreplace_fallback_lock:
            if os.path.lexists(dst):
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dst)
            os.rename(src, dst)
        return
    try:
        os.unlink(src)
    except OSError:
        os.unlink(dst)
        raise


def _is_cross_device_error(err: OSError) -> bool:
    return err.errno == errno.EXDEV or getattr(err, "winerror", None) == 17


def _hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_path(path: str) -> None:
    """Flush a file, or a directory entry on POSIX, to disk; best effort."""
    if IS_WINDOWS and os.path.isdir(path):
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _cross_volume_atomic_move(src: str, dst: str) -> None:
    """Perform atomic non-clobber cross-volume file transfer with transient staging and target verification."""
    dst_dir = os.path.dirname(dst)
    if dst_dir and not os.path.exists(dst_dir):
        os.makedirs(dst_dir, exist_ok=True)

    if os.path.lexists(dst):
        if _is_same_path(src, dst):
            return
        raise FileExistsError(f"Target path already exists: {dst}")

    stage_path = os.path.join(dst_dir, f".tmp_stage_{uuid.uuid4().hex}")
    published = False
    removing_source_tree = False

    try:
        if os.path.isdir(src) and not os.path.islink(src):
            shutil.copytree(src, stage_path)
        elif os.path.islink(src):
            shutil.copy2(src, stage_path, follow_symlinks=False)
        else:
            shutil.copy2(src, stage_path)
            _fsync_path(stage_path)
            # Verify the durable copy before it becomes visible at dst.
            if _hash_file(src) != _hash_file(stage_path):
                raise RuntimeError(
                    f"Target verification failed: copy of {src} does not match the source."
                )

        # Stage and target share a directory, so this rename never crosses devices
        # and fails instead of replacing a file that appeared meanwhile.
        _rename_noreplace(stage_path, dst)
        published = True
        _fsync_path(dst_dir or ".")

        if not os.path.lexists(dst):
            raise RuntimeError(
                f"Target verification failed: {dst} does not exist after move."
            )

        # Target verified! Now safely remove source file/directory.
        if os.path.isdir(src) and not os.path.islink(src):
            removing_source_tree = True
            resilient_rmtree(src)
        else:
            resilient_remove(src)

    except Exception as err:
        # Clean up transient staging file upon failure
        if os.path.lexists(stage_path):
            try:
                if os.path.isdir(stage_path) and not os.path.islink(stage_path):
                    resilient_rmtree(stage_path, ignore_errors=True)
                else:
                    resilient_remove(stage_path)
            except Exception:
                pass
        # Files rmtree already removed from src exist only in dst now, so keep it.
        if removing_source_tree:
            raise PartialMoveError(
                f"Moved {src} to {dst}, but part of the source could not be removed. "
                f"{dst} is complete; the leftover files are still in {src}."
            ) from err
        # Remove a published copy that failed verification; the source is untouched.
        if published and os.path.lexists(src) and os.path.lexists(dst):
            try:
                if os.path.isdir(dst) and not os.path.islink(dst):
                    resilient_rmtree(dst, ignore_errors=True)
                else:
                    resilient_remove(dst)
            except Exception:
                pass
        raise


def atomic_move_non_clobber(src: str, dst: str) -> None:
    """Atomically move src to dst without modifying or replacing target files if dst exists."""
    if _is_same_path(src, dst):
        return

    if os.path.lexists(dst):
        raise FileExistsError(f"Target path already exists: {dst}")

    dst_dir = os.path.dirname(dst)
    if dst_dir and not os.path.exists(dst_dir):
        os.makedirs(dst_dir, exist_ok=True)

    try:
        _rename_noreplace(src, dst)
    except FileExistsError:
        raise
    except OSError as err:
        if not _is_cross_device_error(err):
            raise
        _cross_volume_atomic_move(src, dst)


def resilient_move(src: str, dst: str) -> str:
    """Resiliently move a file or directory using atomic non-clobber moves with dynamic target retry.

    Returns the actual target path where the file was moved.
    """
    if _is_same_path(src, dst):
        return dst

    current_dst = dst
    dest_dir = os.path.dirname(dst) or "."
    orig_filename = os.path.basename(dst)

    from app.core.mover import get_safe_path

    max_attempts = MAX_ATTEMPTS if IS_WINDOWS else 15

    for attempt in range(max_attempts):
        try:
            if not os.path.lexists(src):
                raise FileNotFoundError(f"Source file does not exist: {src}")

            atomic_move_non_clobber(src, current_dst)
            return current_dst

        except FileExistsError as fe:
            current_dst = os.path.normpath(get_safe_path(dest_dir, orig_filename, src))
            if attempt == max_attempts - 1:
                logging.warning(
                    f"Target collision retry limit reached ({max_attempts}) moving {src} to {current_dst}: {fe}"
                )
                raise FileExistsError(f"Target collision retry limit reached: {fe}")

        except (OSError, PermissionError) as e:
            if attempt == max_attempts - 1:
                logging.warning(
                    f"Unrecoverable move conflict moving {src} to {current_dst} after {max_attempts} attempts: {e}"
                )
                raise e

            gc.collect()
            retry_delay = RETRY_DELAY if RETRY_DELAY > 0 else 0.05
            time.sleep(retry_delay)

    return current_dst


@contextmanager
def atomic_quarantine_relocation(
    staged_path: Optional[str | os.PathLike],
    dest_file_path: str | os.PathLike,
):
    """Context-managed resilient file relocation with automatic rollback.

    Relocates a file from `staged_path` to `dest_file_path` on context entry using `resilient_move`.
    If an exception is raised within the context block (e.g., database update failure),
    the file is automatically moved back to `staged_path` before re-raising the exception.
    """
    moved = False
    actual_dest = str(dest_file_path)
    src_str = str(staged_path) if staged_path else None

    if (
        src_str
        and os.path.exists(src_str)
        and os.path.abspath(src_str) != os.path.abspath(actual_dest)
    ):
        actual_dest = resilient_move(src_str, actual_dest)
        moved = True

    try:
        yield actual_dest
    except Exception:
        if moved and os.path.exists(actual_dest) and src_str:
            if not os.path.exists(src_str) or os.path.abspath(
                src_str
            ) != os.path.abspath(actual_dest):
                try:
                    resilient_move(actual_dest, src_str)
                except Exception as rollback_err:
                    logging.error(
                        f"Failed to rollback file relocation from {actual_dest} to {src_str}: {rollback_err}"
                    )
        raise


def resilient_remove(path):
    """Resiliently delete a file, symlink, junction, or empty directory.

    During deletion failures caused by read-only permission locks, the system
    must dynamically adjust file permission attributes to allow successful cleanup.
    """
    for attempt in range(MAX_ATTEMPTS):
        try:
            if is_junction_path(path) or os.path.islink(path):
                try:
                    os.unlink(path)
                except OSError:
                    os.rmdir(path)
            elif os.path.isdir(path):
                os.rmdir(path)
            else:
                os.remove(path)
            return
        except (OSError, PermissionError) as e:
            # During deletion failures caused by read-only permission locks,
            # dynamically adjust file permission attributes.
            try:
                os.chmod(path, stat.S_IWRITE)
            except Exception:
                pass

            try:
                # Try again immediately after chmod within the same attempt
                if is_junction_path(path) or os.path.islink(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        os.rmdir(path)
                elif os.path.isdir(path):
                    os.rmdir(path)
                else:
                    os.remove(path)
                return
            except (OSError, PermissionError):
                pass

            if attempt == MAX_ATTEMPTS - 1:
                logging.error(
                    f"Failed to remove path {path} after {MAX_ATTEMPTS} attempts: {e}"
                )
                raise e

            # Force a garbage collection cycle immediately before every retry attempt
            gc.collect()
            if RETRY_DELAY > 0:
                time.sleep(RETRY_DELAY)


def resilient_rmtree(path, ignore_errors=False):
    """Resiliently delete a directory tree, adjusting permissions on write-permission locks."""
    if not os.path.lexists(path):
        return

    if is_junction_path(path) or os.path.islink(path):
        try:
            resilient_remove(path)
        except Exception:
            if not ignore_errors:
                raise
        return

    def _handle_error(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        except Exception:
            pass
        parent = os.path.dirname(p)
        if parent and os.path.exists(parent):
            try:
                os.chmod(parent, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except Exception:
                pass
        try:
            func(p)
        except Exception:
            pass

    max_attempts = 1 if ignore_errors else MAX_ATTEMPTS
    for attempt in range(max_attempts):
        try:
            shutil.rmtree(path, onexc=_handle_error)
            if not os.path.lexists(path):
                return
            raise OSError(f"Directory {path} was not deleted")
        except (OSError, PermissionError) as e:
            if attempt == max_attempts - 1:
                logging.warning(
                    f"Failed to rmtree {path} after {max_attempts} attempts: {e}"
                )
                if ignore_errors:
                    return
                raise e

            gc.collect()
            if RETRY_DELAY > 0:
                time.sleep(RETRY_DELAY)


def resilient_file_hash(
    file_path: str | os.PathLike,
    skip_media_tags: bool = False,
    chunk_size: int = 65536,
    normalize_text: bool = False,
) -> str:
    """Resiliently calculate SHA-256 hash of a file with unified retry schedule.

    File hashing operations bypass retry cycles entirely on macOS and Linux.
    For MP3 and M4A files, optionally skips metadata headers and structural atoms
    to isolate raw audio payload.
    """
    import hashlib
    import struct

    str_path = os.fspath(file_path)

    for attempt in range(MAX_ATTEMPTS):
        hasher = hashlib.sha256()
        success = False

        try:
            if normalize_text:
                try:
                    with open(str_path, "r", encoding="utf-8-sig", newline=None) as f:
                        content = f.read()
                    normalized_bytes = content.replace("\r\n", "\n").encode("utf-8")
                    hasher.update(normalized_bytes)
                    success = True
                except UnicodeDecodeError:
                    with open(str_path, "rb") as f:
                        while True:
                            chunk = f.read(chunk_size)
                            if not chunk:
                                break
                            hasher.update(chunk)
                    success = True
            elif skip_media_tags:
                ext = os.path.splitext(str_path)[1].lower()

                if ext in (".docx", ".xlsx"):
                    import zipfile

                    with zipfile.ZipFile(str_path, "r") as zf:
                        entries = [
                            info
                            for info in zf.infolist()
                            if not info.is_dir()
                            and not info.filename.replace("\\", "/")
                            .lower()
                            .lstrip("/")
                            .startswith("docprops/")
                            and info.filename.replace("\\", "/").lower().lstrip("/")
                            != "docprops"
                        ]
                        entries.sort(key=lambda x: x.filename)
                        for info in entries:
                            hasher.update(info.filename.encode("utf-8"))
                            hasher.update(zf.read(info.filename))
                    success = True
                else:
                    offset = 0
                    size_to_hash = -1  # -1 means hash to EOF

                    if ext == ".mp3":
                        with open(str_path, "rb") as f:
                            while True:
                                header = f.read(10)
                                if len(header) >= 10 and header[:3] == b"ID3":
                                    flags = header[5]
                                    size = (
                                        (header[6] << 21)
                                        | (header[7] << 14)
                                        | (header[8] << 7)
                                        | header[9]
                                    )
                                    has_footer = (flags & 0x10) != 0
                                    tag_size = 10 + size + (10 if has_footer else 0)
                                    offset += tag_size
                                    f.seek(tag_size - 10, 1)
                                else:
                                    break
                    elif ext == ".m4a":
                        with open(str_path, "rb") as f:
                            while True:
                                header = f.read(8)
                                if len(header) < 8:
                                    break
                                box_size, box_type = struct.unpack(">I4s", header)
                                header_size = 8

                                if box_size == 1:
                                    box_size = struct.unpack(">Q", f.read(8))[0]
                                    header_size = 16
                                elif box_size == 0:
                                    if box_type == b"mdat":
                                        offset = f.tell()
                                        size_to_hash = -1
                                    break

                                if box_type == b"mdat":
                                    offset = f.tell()
                                    size_to_hash = box_size - header_size
                                    break

                                f.seek(box_size - header_size, os.SEEK_CUR)

                    with open(str_path, "rb") as f:
                        if offset > 0:
                            f.seek(offset)

                        bytes_remaining = size_to_hash

                        while True:
                            if bytes_remaining != -1:
                                read_size = min(chunk_size, bytes_remaining)
                                if read_size <= 0:
                                    break
                            else:
                                read_size = chunk_size

                            chunk = f.read(read_size)
                            if not chunk:
                                break

                            hasher.update(chunk)
                            if bytes_remaining != -1:
                                bytes_remaining -= len(chunk)
                    success = True
            else:
                with open(str_path, "rb") as f:
                    while True:
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        hasher.update(chunk)
                success = True
        except (OSError, PermissionError) as e:
            if attempt == MAX_ATTEMPTS - 1:
                logging.error(
                    f"Failed to calculate hash of {file_path} after {MAX_ATTEMPTS} attempts: {e}"
                )
                break

            gc.collect()
            if RETRY_DELAY > 0:
                time.sleep(RETRY_DELAY)
            continue
        except Exception:
            # Fallback to standard whole-file hashing if parsing fails
            try:
                hasher = hashlib.sha256()
                with open(str_path, "rb") as f:
                    while True:
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        hasher.update(chunk)
                success = True
            except (OSError, PermissionError) as e:
                if attempt == MAX_ATTEMPTS - 1:
                    logging.error(
                        f"Failed to calculate fallback hash of {file_path} after {MAX_ATTEMPTS} attempts: {e}"
                    )
                    break

                gc.collect()
                if RETRY_DELAY > 0:
                    time.sleep(RETRY_DELAY)
                continue

        if success:
            return hasher.hexdigest()

    return hashlib.sha256().hexdigest()


def same_file_content(path_a: str, path_b: str) -> bool:
    """Return True only when both paths are one file or regular files with identical bytes.

    Compares raw bytes, not the metadata-insensitive digest ``get_file_hash`` uses,
    and returns False whenever either file cannot be read.
    """
    try:
        if os.path.samefile(path_a, path_b):
            return True
    except OSError:
        return False
    if os.path.islink(path_a) or os.path.islink(path_b):
        return False
    if not (os.path.isfile(path_a) and os.path.isfile(path_b)):
        return False
    try:
        if os.path.getsize(path_a) != os.path.getsize(path_b):
            return False
        return _hash_file(path_a) == _hash_file(path_b)
    except OSError:
        return False


def _set_posix_mode(path: str, mode: int) -> None:
    """Set POSIX file permissions mode if on non-Windows platform."""
    if sys.platform != "win32":
        try:
            os.chmod(path, mode)
        except OSError:
            pass


_BINARY_CONTAINER_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".docx",
        ".doc",
        ".xlsx",
        ".xls",
        ".pptx",
        ".ppt",
        ".odt",
        ".ods",
        ".odp",
        ".epub",
        ".rtf",
        ".msg",
        ".zip",
        ".tar",
        ".gz",
        ".tgz",
        ".bz2",
        ".tbz2",
        ".xz",
        ".txz",
        ".7z",
        ".rar",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".tiff",
        ".tif",
        ".ico",
        ".heic",
        ".mp3",
        ".m4a",
        ".wav",
        ".flac",
        ".ogg",
        ".mp4",
        ".mov",
        ".avi",
        ".mkv",
        ".webm",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".iso",
        ".dmg",
        ".sqlite",
        ".sqlite3",
        ".db",
        ".parquet",
        ".feather",
        ".onnx",
        ".safetensors",
    }
)

_BINARY_MAGIC_PREFIXES = (
    b"%PDF-",
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
    b"7z\xbc\xaf\x27\x1c",
    b"Rar!\x1a\x07",
    b"\x1f\x8b\x08",
    b"BZh",
    b"\xfd7zXZ\x00",
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"SQLite format 3\x00",
    b"\x7fELF",
    b"MZ",
)


def is_verified_utf8_text_file(
    file_path: str | os.PathLike,
) -> tuple[bool, str | None]:
    """Return ``(True, decoded_text)`` only if ``file_path`` is verified plain-text UTF-8.

    Rejects rich binary document containers (such as ``.pdf``, ``.docx``, ``.xlsx``,
    archives, images, and executables), binary magic headers, files containing NUL
    bytes, and non-UTF-8 byte sequences so binary layouts are never corrupted.
    """
    str_path = os.fspath(file_path)
    lower_name = os.path.basename(str_path).lower()
    if lower_name.endswith(".tar.gz"):
        return False, None
    _, ext = os.path.splitext(lower_name)
    if ext in _BINARY_CONTAINER_EXTENSIONS:
        return False, None

    try:
        with open(str_path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return False, None

    if any(raw.startswith(prefix) for prefix in _BINARY_MAGIC_PREFIXES):
        return False, None
    if b"\x00" in raw:
        return False, None

    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False, None

    return True, decoded


def resilient_verified_copy(
    src: str | os.PathLike,
    dst: str | os.PathLike,
    *,
    allow_rename_on_collision: bool = True,
) -> tuple[str, str, str]:
    """Copy ``src`` to ``dst`` via a staged temp file, verifying SHA-256 before publishing.

    Leaves ``src`` untouched at origin and returns ``(actual_dst, src_sha256, dst_sha256)``.
    """
    from app.core.mover import get_safe_path

    src_str = os.fspath(src)
    dst_str = os.fspath(dst)
    if not os.path.lexists(src_str):
        raise FileNotFoundError(f"Source file does not exist: {src_str}")

    src_sha = _hash_file(src_str)
    if _is_same_path(src_str, dst_str):
        if not allow_rename_on_collision:
            return dst_str, src_sha, src_sha
        dest_dir_same = os.path.dirname(dst_str) or "."
        base, ext = os.path.splitext(os.path.basename(dst_str))
        dst_str = get_safe_path(dest_dir_same, f"{base}_copy{ext}")

    dest_dir = os.path.dirname(dst_str) or "."
    os.makedirs(dest_dir, exist_ok=True)
    orig_filename = os.path.basename(dst_str)
    current_dst = dst_str

    max_attempts = MAX_ATTEMPTS if IS_WINDOWS else 15
    for attempt in range(max_attempts):
        stage_path = os.path.join(dest_dir, f".tmp_copy_{uuid.uuid4().hex}")
        try:
            shutil.copy2(src_str, stage_path)
            _fsync_path(stage_path)
            staged_sha = _hash_file(stage_path)
            if staged_sha != src_sha:
                raise RuntimeError(
                    f"Copy hash verification failed for {src_str} -> {current_dst}"
                )
            _rename_noreplace(stage_path, current_dst)
            _fsync_path(dest_dir)
            final_sha = _hash_file(current_dst)
            if final_sha != src_sha:
                resilient_remove(current_dst)
                raise RuntimeError(
                    f"Published copy hash mismatch for {src_str} -> {current_dst}"
                )
            return current_dst, src_sha, final_sha
        except FileExistsError:
            if os.path.lexists(stage_path):
                resilient_remove(stage_path)
            if not allow_rename_on_collision or attempt == max_attempts - 1:
                raise
            current_dst = os.path.normpath(get_safe_path(dest_dir, orig_filename, None))
        except Exception:
            if os.path.lexists(stage_path):
                try:
                    resilient_remove(stage_path)
                except OSError:
                    pass
            raise

    raise RuntimeError(f"Exhausted copy collision attempts for {src_str} -> {dst_str}")


def create_verified_zip_archive(
    src: str | os.PathLike,
    dst_zip: str | os.PathLike,
    *,
    arcname: str | None = None,
    allow_rename_on_collision: bool = True,
) -> tuple[str, str, str]:
    """Compress ``src`` into a verified stdlib ``.zip`` archive at ``dst_zip``.

    Verifies uncompressed member SHA-256 and ``ZipFile.testzip()`` before publishing,
    leaving ``src`` untouched on disk until the caller records or parks it.
    Returns ``(actual_dst_zip, src_sha256, dst_zip_sha256)``.
    """
    import zipfile

    from app.core.mover import get_safe_path

    src_str = os.fspath(src)
    dst_str = os.fspath(dst_zip)
    if not os.path.isfile(src_str):
        raise FileNotFoundError(f"Source file does not exist for archiving: {src_str}")

    if not dst_str.lower().endswith(".zip"):
        dst_str = f"{dst_str}.zip"

    src_sha = _hash_file(src_str)
    member_name = arcname or os.path.basename(src_str)
    dest_dir = os.path.dirname(dst_str) or "."
    os.makedirs(dest_dir, exist_ok=True)
    orig_zip_name = os.path.basename(dst_str)
    current_dst = dst_str

    max_attempts = MAX_ATTEMPTS if IS_WINDOWS else 15
    for attempt in range(max_attempts):
        stage_zip = os.path.join(dest_dir, f".tmp_archive_{uuid.uuid4().hex}.zip")
        try:
            with zipfile.ZipFile(
                stage_zip, mode="w", compression=zipfile.ZIP_DEFLATED
            ) as zf:
                zf.write(src_str, arcname=member_name)
            _fsync_path(stage_zip)

            with zipfile.ZipFile(stage_zip, mode="r") as zf_check:
                bad_member = zf_check.testzip()
                if bad_member is not None:
                    raise RuntimeError(
                        f"ZIP integrity check failed on member '{bad_member}'"
                    )
                digest = hashlib.sha256()
                with zf_check.open(member_name, "r") as member_fh:
                    for block in iter(lambda: member_fh.read(1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest() != src_sha:
                    raise RuntimeError(
                        f"Archived member SHA-256 mismatch for '{src_str}'"
                    )

            zip_sha = _hash_file(stage_zip)
            _rename_noreplace(stage_zip, current_dst)
            _fsync_path(dest_dir)
            if _hash_file(current_dst) != zip_sha:
                resilient_remove(current_dst)
                raise RuntimeError(
                    f"Published ZIP SHA-256 mismatch for '{current_dst}'"
                )
            return current_dst, src_sha, zip_sha
        except FileExistsError:
            if os.path.lexists(stage_zip):
                resilient_remove(stage_zip)
            if not allow_rename_on_collision or attempt == max_attempts - 1:
                raise
            current_dst = os.path.normpath(get_safe_path(dest_dir, orig_zip_name, None))
        except Exception:
            if os.path.lexists(stage_zip):
                try:
                    resilient_remove(stage_zip)
                except OSError:
                    pass
            raise

    raise RuntimeError(
        f"Exhausted archive collision attempts for {src_str} -> {dst_str}"
    )


def write_verified_utf8_file(
    content: str,
    dst: str | os.PathLike,
    *,
    mode: int = 0o600,
    allow_rename_on_collision: bool = True,
) -> tuple[str, str]:
    """Write ``content`` as UTF-8 via a staged temp file, fsync, verify SHA-256, and publish.

    Returns ``(actual_dst, dst_sha256)``.
    """
    from app.core.mover import get_safe_path

    dst_str = os.fspath(dst)
    dest_dir = os.path.dirname(dst_str) or "."
    os.makedirs(dest_dir, exist_ok=True)
    payload = content.encode("utf-8")
    expected_sha = hashlib.sha256(payload).hexdigest()
    orig_filename = os.path.basename(dst_str)
    current_dst = dst_str

    max_attempts = MAX_ATTEMPTS if IS_WINDOWS else 15
    for attempt in range(max_attempts):
        stage_path = os.path.join(dest_dir, f".tmp_utf8_{uuid.uuid4().hex}")
        try:
            with open(stage_path, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            _set_posix_mode(stage_path, mode)
            if _hash_file(stage_path) != expected_sha:
                raise RuntimeError(f"Staged UTF-8 write verification failed: {dst_str}")
            _rename_noreplace(stage_path, current_dst)
            _fsync_path(dest_dir)
            if _hash_file(current_dst) != expected_sha:
                resilient_remove(current_dst)
                raise RuntimeError(
                    f"Published UTF-8 write verification failed: {current_dst}"
                )
            return current_dst, expected_sha
        except FileExistsError:
            if os.path.lexists(stage_path):
                resilient_remove(stage_path)
            if not allow_rename_on_collision or attempt == max_attempts - 1:
                raise
            current_dst = os.path.normpath(get_safe_path(dest_dir, orig_filename, None))
        except Exception:
            if os.path.lexists(stage_path):
                try:
                    resilient_remove(stage_path)
                except OSError:
                    pass
            raise

    raise RuntimeError(
        f"Exhausted UTF-8 write collision attempts for destination '{dst_str}'"
    )
