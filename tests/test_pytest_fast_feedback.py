"""Regression tests for serial-by-default pytest, collection imports, and teardown overhead."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _clean_pytest_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_XDIST_WORKER", None)
    return env


def test_pyproject_default_addopts_is_serial_loadgroup():
    """Default addopts must preserve loadgroup scheduling without forcing -n auto."""
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    addopts = pyproject["tool"]["pytest"]["ini_options"]["addopts"]
    assert addopts == "--dist=loadgroup"
    assert "-n" not in addopts.split()


def test_missing_test_path_exits_with_code_4_and_stderr(tmp_path):
    """Mistyped test paths must exit immediately with pytest usage error 4 and stderr output."""
    missing_path = tmp_path / "does_not_exist_test.py"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(missing_path)],
        cwd=REPO_ROOT,
        env=_clean_pytest_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 4
    assert "does_not_exist_test.py" in result.stderr


def test_xdist_flag_honors_loadgroup_scheduling(tmp_path):
    """Default runs execute serially in-process, while -n activates LoadGroupScheduling."""
    out_file = tmp_path / "worker.txt"
    sample_test = tmp_path / "test_group_routing.py"
    sample_test.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "import pytest\n\n"
        f"OUT = Path({str(out_file)!r})\n\n"
        "@pytest.mark.xdist_group(name='shared_group')\n"
        "def test_group_member_a():\n"
        "    OUT.write_text(os.environ.get('PYTEST_XDIST_WORKER', 'serial'), encoding='utf-8')\n\n"
        "@pytest.mark.xdist_group(name='shared_group')\n"
        "def test_group_member_b():\n"
        "    assert os.environ.get('PYTEST_XDIST_WORKER', 'serial') != ''\n",
        encoding="utf-8",
    )

    serial_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(REPO_ROOT / "pyproject.toml"),
            "-v",
            str(sample_test),
        ],
        cwd=tmp_path,
        env=_clean_pytest_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert serial_result.returncode == 0, serial_result.stdout + serial_result.stderr
    assert out_file.read_text(encoding="utf-8") == "serial"
    assert "LoadGroupScheduling" not in serial_result.stdout

    parallel_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(REPO_ROOT / "pyproject.toml"),
            "-n",
            "2",
            "-v",
            str(sample_test),
        ],
        cwd=tmp_path,
        env=_clean_pytest_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert parallel_result.returncode == 0, (
        parallel_result.stdout + parallel_result.stderr
    )
    assert "scheduling tests via LoadGroupScheduling" in parallel_result.stdout
    assert out_file.read_text(encoding="utf-8").startswith("gw")


def test_fast_suite_collection_defers_sklearn_and_hypothesis():
    """Collecting `pytest -m 'not slow'` must not load sklearn or hypothesis."""
    probe = (
        "import json, sys, pytest\n"
        "loaded = {}\n"
        "class _Probe:\n"
        "    def pytest_collection_finish(self, session):\n"
        "        loaded['sklearn'] = any(m == 'sklearn' or m.startswith('sklearn.') for m in sys.modules)\n"
        "        loaded['hypothesis'] = any(m == 'hypothesis' or m.startswith('hypothesis.') for m in sys.modules)\n"
        "        loaded['count'] = len(session.items)\n"
        "rc = pytest.main(['--collect-only', '-q', '-m', 'not slow', 'tests'], plugins=[_Probe()])\n"
        "print('PROBE_JSON:' + json.dumps({'rc': int(rc), **loaded}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=_clean_pytest_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    marker_lines = [
        line.removeprefix("PROBE_JSON:")
        for line in result.stdout.splitlines()
        if line.startswith("PROBE_JSON:")
    ]
    assert marker_lines, result.stdout + result.stderr
    payload = json.loads(marker_lines[-1])
    assert payload["rc"] == 0
    assert payload["count"] > 0
    assert payload["sklearn"] is False
    assert payload["hypothesis"] is False


def test_autouse_teardown_clears_cache_once_and_skips_unimported_singletons():
    """A lightweight unit test must clear the DB cache once and avoid importing optional subsystems."""
    probe = (
        "import json, sys, pytest\n"
        "from app.core import db_conn\n"
        "calls = []\n"
        "orig = db_conn.clear_connection_cache\n"
        "def _wrapped(*args, **kwargs):\n"
        "    calls.append((args, kwargs))\n"
        "    return orig(*args, **kwargs)\n"
        "db_conn.clear_connection_cache = _wrapped\n"
        "rc = pytest.main(['-q', 'tests/test_db_conn.py::test_escape_pragma_key_unit'])\n"
        "optional = [\n"
        "    'app.core.downloader',\n"
        "    'app.core.model_registry',\n"
        "    'app.core.services',\n"
        "    'app.core.worker_pool',\n"
        "]\n"
        "imported_optional = [m for m in optional if m in sys.modules]\n"
        "print('PROBE_JSON:' + json.dumps({\n"
        "    'rc': int(rc),\n"
        "    'clear_calls': len(calls),\n"
        "    'imported_optional': imported_optional,\n"
        "}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=_clean_pytest_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    marker_lines = [
        line.removeprefix("PROBE_JSON:")
        for line in result.stdout.splitlines()
        if line.startswith("PROBE_JSON:")
    ]
    assert marker_lines, result.stdout + result.stderr
    payload = json.loads(marker_lines[-1])
    assert payload["rc"] == 0
    assert payload["clear_calls"] == 1
    assert payload["imported_optional"] == []
