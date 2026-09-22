"""Unit and integration tests for headless build profiles and optional dependency extras."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest


def test_a11y_gate_headless_skip():
    """Verify that run_a11y_gate.py exits gracefully with code 0 in headless build mode."""
    from scripts import run_a11y_gate

    with patch.dict(os.environ, {"HEADLESS_BUILD": "1"}):
        with pytest.raises(SystemExit) as exc_info:
            run_a11y_gate.main()
        assert exc_info.value.code == 0


def test_a11y_gate_no_nicegui():
    """Verify that run_a11y_gate.py exits gracefully with code 0 when nicegui is missing."""
    from scripts import run_a11y_gate

    with patch("importlib.util.find_spec", return_value=None):
        with patch.dict(os.environ, {"HEADLESS_BUILD": "0"}, clear=False):
            assert run_a11y_gate.is_gui_available() is False
            with pytest.raises(SystemExit) as exc_info:
                run_a11y_gate.main()
            assert exc_info.value.code == 0


def test_build_script_headless_flag():
    """Verify that scripts/build.py parses --headless flag and sets HEADLESS_BUILD=1."""
    from scripts import build

    test_args = ["build.py", "--lite", "--headless"]
    with patch.object(sys, "argv", test_args):
        with patch.dict(os.environ, {}, clear=False):
            with patch("PyInstaller.__main__.run") as mock_pyi_run:
                with patch.object(build, "update_binaries_and_manifest"):
                    with patch.object(build, "download_and_prepare_weights"):
                        with patch("importlib.util.find_spec", return_value=MagicMock()):
                            build.main()
                            assert os.environ.get("HEADLESS_BUILD") == "1"
                            mock_pyi_run.assert_called_once()


def test_spec_headless_asset_exclusion():
    """Verify that smart-autosorter.spec filters NiceGUI assets and includes nicegui in excludes when HEADLESS_BUILD=1."""
    spec_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "smart-autosorter.spec"
    )
    assert os.path.exists(spec_path)

    with open(spec_path, "r", encoding="utf-8") as f:
        spec_content = f.read()

    mock_globals = {
        "Analysis": MagicMock(),
        "PYZ": MagicMock(),
        "EXE": MagicMock(),
        "COLLECT": MagicMock(),
        "__file__": spec_path,
    }

    mock_hooks = MagicMock()
    mock_hooks.collect_all.return_value = ([], [], [])

    with patch.dict(os.environ, {"HEADLESS_BUILD": "1"}):
        with patch.dict(
            sys.modules,
            {
                "PyInstaller": MagicMock(),
                "PyInstaller.utils": MagicMock(),
                "PyInstaller.utils.hooks": mock_hooks,
            },
        ):
            exec(spec_content, mock_globals)

            is_nicegui_asset = mock_globals["is_nicegui_asset"]
            assert is_nicegui_asset("site-packages/nicegui/elements/lib/quasar.js") is True
            assert is_nicegui_asset("site-packages/nicegui/static/index.html") is True
            assert is_nicegui_asset("app/core/scanner.py") is False

            excludes = mock_globals["excludes"]
            assert "nicegui" in excludes
            assert "fastapi" in excludes
            assert "uvicorn" in excludes


def test_main_gui_missing_nicegui_error(capsys):
    """Verify that launching GUI mode without nicegui outputs diagnostic message and exits with code 1."""
    from app import main

    test_args = ["smart-autosorter", "--gui"]
    with patch.object(sys, "argv", test_args):
        with patch.dict(sys.modules, {"app.ui.app": None}):
            with pytest.raises(SystemExit) as exc_info:
                main.main()
            assert exc_info.value.code == 1

            captured = capsys.readouterr()
            assert "NiceGUI web interface dependencies are not installed" in captured.err
            assert "smart-autosorter[gui]" in captured.err
