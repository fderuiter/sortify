"""Centralized helper for child process environment sanitization and safe spawning.

Ensures that child processes spawned on Windows run without infinite subprocess
loops, use a clean environment stripped of PyInstaller reference variables,
can resolve decoupled modules from the local cache, and do not pop up terminal windows.
"""

import os
import subprocess
import sys

from app.config import get_app_dir


def get_cleaned_env(env: dict = None) -> dict:
    """Return a copy of the environment dictionary with PyInstaller-specific variables removed."""
    if env is None:
        env = os.environ.copy()
    else:
        env = env.copy()

    # Remove package-specific variables (such as _MEIPASS and PYTHONPATH)
    env.pop("_MEIPASS", None)
    env.pop("PYTHONPATH", None)

    # Explicitly inject the local cache directory into the search path (PYTHONPATH)
    cache_dir = get_app_dir() / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    env["PYTHONPATH"] = str(cache_dir)

    return env


def get_subprocess_startupinfo():
    """Return a STARTUPINFO object that hides the console window on Windows."""
    if sys.platform == "win32":
        if hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE
            return startupinfo
    return None


def spawn_background_process(cmd, **kwargs):
    """Spawn a background process asynchronously using the cleaned environment and hiding terminal window on Windows."""
    if "env" not in kwargs:
        kwargs["env"] = get_cleaned_env()
    else:
        kwargs["env"] = get_cleaned_env(kwargs["env"])

    if "startupinfo" not in kwargs and sys.platform == "win32":
        kwargs["startupinfo"] = get_subprocess_startupinfo()

    return subprocess.Popen(cmd, **kwargs)


def run_background_process(cmd, **kwargs):
    """Run a background process synchronously with the cleaned environment and hiding terminal window on Windows."""
    if "env" not in kwargs:
        kwargs["env"] = get_cleaned_env()
    else:
        kwargs["env"] = get_cleaned_env(kwargs["env"])

    if "startupinfo" not in kwargs and sys.platform == "win32":
        kwargs["startupinfo"] = get_subprocess_startupinfo()

    return subprocess.run(cmd, **kwargs)
