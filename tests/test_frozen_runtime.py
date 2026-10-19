"""Tests for the packaged Windows runtime setup in app.frozen_runtime."""

import importlib
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from app import frozen_runtime


@pytest.fixture(autouse=True)
def _reset_prepared(monkeypatch):
    monkeypatch.setattr(frozen_runtime, "_prepared", False)


def test_importing_main_has_no_frozen_side_effects():
    """Importing the entry module must not touch streams, DLL paths or db_conn."""
    code = (
        "import sys, app.main; "
        "assert 'app.core.db_conn' not in sys.modules; "
        "assert 'sqlcipher3' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_prepare_is_a_noop_outside_frozen_windows():
    with (
        patch.object(frozen_runtime, "attach_console") as attach,
        patch.object(frozen_runtime, "_add_dll_directory") as add_dll,
    ):
        frozen_runtime.prepare_frozen_windows_runtime()
    attach.assert_not_called()
    add_dll.assert_not_called()


def test_prepare_fills_missing_streams_and_registers_dlls(tmp_path, monkeypatch):
    base = tmp_path / "bundle"
    nested = base / "_internal" / "sqlcipher3" / "libs"
    nested.mkdir(parents=True)
    exe_dir = tmp_path / "exe"
    exe_dir.mkdir()

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(exe_dir / "app.exe"))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setenv("PATH", "ORIGINAL")
    add_dll = MagicMock()

    with (
        patch.object(frozen_runtime, "is_packaged", return_value=True),
        patch.object(frozen_runtime, "get_meipass_dir", return_value=str(base)),
        patch.object(frozen_runtime, "attach_console", return_value=False),
        patch.object(frozen_runtime, "_add_dll_directory", add_dll),
    ):
        frozen_runtime.prepare_frozen_windows_runtime()
        # A second call does nothing.
        frozen_runtime.prepare_frozen_windows_runtime()

    assert isinstance(sys.stdout, frozen_runtime.NullWriter)
    assert isinstance(sys.stderr, frozen_runtime.NullWriter)
    assert isinstance(sys.stdin, frozen_runtime.NullReader)

    registered = [c.args[0] for c in add_dll.call_args_list]
    assert registered == [
        str(base),
        str(base / "_internal"),
        str(base / "_internal" / "sqlcipher3"),
        str(nested),
        str(exe_dir),
    ]
    assert os.environ["PATH"] == ";".join(registered) + ";ORIGINAL"


def test_attach_console_returns_false_without_windll():
    ctypes = importlib.import_module("ctypes")
    with patch.object(ctypes, "windll", create=True, new=None):
        assert frozen_runtime.attach_console() is False
