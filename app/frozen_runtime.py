"""Process setup for the packaged (PyInstaller) Windows build.

Importing this module has no side effects. The entry point calls
``prepare_frozen_windows_runtime()`` before anything loads native libraries;
it is a no-op everywhere except a frozen Windows executable.
"""

import contextlib
import os
import sys

from app.core.path_utils import get_meipass_dir, is_packaged


class NullWriter:
    """A helper class that discards any written output to mimic a stream."""

    encoding = "utf-8"
    errors = "replace"

    def write(self, text):
        """Discard written text."""
        return len(text) if text else 0

    def writelines(self, lines):
        """Discard written lines."""

    def flush(self):
        """No-op flush to satisfy the stream interface."""

    def isatty(self):
        """Return False for null stream."""
        return False

    def fileno(self):
        """Raise OSError for missing file descriptor."""
        raise OSError("NullWriter has no file descriptor")

    def readable(self):
        """Return False for write-only null stream."""
        return False

    def writable(self):
        """Return True for null writer stream."""
        return True

    def seekable(self):
        """Return False for null stream."""
        return False

    @property
    def closed(self):
        """Return False for active null stream."""
        return False


class NullReader:
    """A helper class that discards input stream calls."""

    encoding = "utf-8"
    errors = "replace"

    def read(self, *args, **kwargs):
        """Return empty string."""
        return ""

    def readline(self, *args, **kwargs):
        """Return empty string."""
        return ""

    def readlines(self, *args, **kwargs):
        """Return empty list."""
        return []

    def isatty(self):
        """Return False for null stream."""
        return False

    def fileno(self):
        """Raise OSError for missing file descriptor."""
        raise OSError("NullReader has no file descriptor")

    def readable(self):
        """Return True for null reader stream."""
        return True

    def writable(self):
        """Return False for null reader stream."""
        return False

    def seekable(self):
        """Return False for null stream."""
        return False

    @property
    def closed(self):
        """Return False for active null stream."""
        return False


_prepared = False


def attach_console() -> bool:
    """Allocate a Windows console and point the standard streams at it.

    Windowed executables (``console=False``) start without one. Returns True
    if a console was allocated; each stream that cannot be opened is left as
    it was.
    """
    try:
        import ctypes

        allocated = bool(ctypes.windll.kernel32.AllocConsole())  # ty: ignore[unresolved-attribute]
    except (AttributeError, OSError):
        return False
    if not allocated:
        return False
    with contextlib.suppress(OSError):
        sys.stdout = open("CONOUT$", "w", encoding="utf-8")  # noqa: SIM115
    with contextlib.suppress(OSError):
        sys.stderr = open("CONERR$", "w", encoding="utf-8")  # noqa: SIM115
    with contextlib.suppress(OSError):
        sys.stdin = open("CONIN$", encoding="utf-8")  # noqa: SIM115
    return True


def _add_dll_directory(path: str) -> None:
    # A directory that vanished or is not allowed only loses that search path.
    with contextlib.suppress(OSError):
        os.add_dll_directory(path)  # ty: ignore[unresolved-attribute]


def bundle_dll_directories(base_dir: str | None, exe_dir: str | None) -> list[str]:
    """Return the existing bundle directories that may hold native DLLs.

    Covers the PyInstaller extraction dir, its ``_internal`` folder (6+), the
    bundled ``sqlcipher3`` package and every folder under it, and the
    executable's own directory. Order is preserved and duplicates dropped.
    """
    candidates: list[str] = []
    if base_dir:
        base_dir = os.path.abspath(base_dir)
        candidates += [base_dir, os.path.join(base_dir, "_internal")]
        for sqlcipher_dir in (
            os.path.join(base_dir, "sqlcipher3"),
            os.path.join(base_dir, "_internal", "sqlcipher3"),
            os.path.join(base_dir, "app", "binaries", "windows", "sqlcipher3"),
            os.path.join(
                base_dir, "_internal", "app", "binaries", "windows", "sqlcipher3"
            ),
        ):
            if os.path.isdir(sqlcipher_dir):
                candidates.append(sqlcipher_dir)
                for root, dirs, _ in os.walk(sqlcipher_dir):
                    candidates += [os.path.join(root, d) for d in dirs]
    if exe_dir:
        exe_dir = os.path.abspath(exe_dir)
        candidates += [exe_dir, os.path.join(exe_dir, "_internal")]

    unique: list[str] = []
    for path in candidates:
        path = os.path.abspath(path)
        if path not in unique and os.path.isdir(path):
            unique.append(path)
    return unique


def prepare_frozen_windows_runtime() -> None:
    """Make a frozen Windows executable safe to run. Idempotent.

    Gives a windowed build real (or null) standard streams, and registers the
    bundle's DLL folders so ``sqlcipher3`` and other native extensions load.
    Must run before anything imports ``app.core.db_conn``.
    """
    global _prepared
    if _prepared or sys.platform != "win32" or not is_packaged():
        return
    _prepared = True

    if sys.stdout is None or sys.stderr is None or sys.stdin is None:
        attach_console()
    if sys.stdout is None:
        sys.stdout = NullWriter()
    if sys.stderr is None:
        sys.stderr = NullWriter()
    if sys.stdin is None:
        sys.stdin = NullReader()

    base_dir = get_meipass_dir()
    dll_dirs = bundle_dll_directories(base_dir, os.path.dirname(sys.executable) or None)
    for path in dll_dirs:
        _add_dll_directory(path)
    # Prepend them to PATH too, so OS-level resolution of dependent DLLs works.
    if base_dir and dll_dirs:
        os.environ["PATH"] = ";".join(dll_dirs) + ";" + os.environ.get("PATH", "")
