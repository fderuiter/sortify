import sys
from unittest.mock import MagicMock, patch

import pytest

# Ensure PyInstaller is mocked before importing main
mock_pyinstaller = MagicMock()
mock_pyinstaller_main = MagicMock()
mock_pyinstaller.__main__ = mock_pyinstaller_main
sys.modules["PyInstaller"] = mock_pyinstaller
sys.modules["PyInstaller.__main__"] = mock_pyinstaller_main

from scripts.build import main  # noqa: E402


def test_build_script_lite_no_checks():
    """Verify that a lite build skips the heavy ML checks entirely."""
    mock_pyinstaller_main.run.reset_mock()
    with patch("sys.argv", ["build.py", "--lite"]):
        with (
            patch("importlib.util.find_spec") as mock_find_spec,
            patch("sys.exit", side_effect=SystemExit) as mock_exit,
        ):
            # Setup sqlcipher3 mock so it passes
            mock_spec = MagicMock()
            mock_spec.submodule_search_locations = ["/some/path"]
            mock_find_spec.return_value = mock_spec

            main()

            # Since lite is enabled, we should NOT have searched for 'torch'
            # Let's verify that 'torch' is not checked.
            checked_modules = [call.args[0] for call in mock_find_spec.call_args_list]
            assert "torch" not in checked_modules
            mock_exit.assert_not_called()
            mock_pyinstaller_main.run.assert_called_once_with(
                ["smart-autosorter.spec", "--noconfirm", "--clean"]
            )


def test_build_script_standard_all_present():
    """Verify that a standard build passes if all ML packages are present."""
    mock_pyinstaller_main.run.reset_mock()
    with patch("sys.argv", ["build.py"]):
        with (
            patch("importlib.util.find_spec") as mock_find_spec,
            patch("sys.exit", side_effect=SystemExit) as mock_exit,
        ):
            # All find_spec calls return a valid spec
            mock_spec = MagicMock()
            mock_spec.submodule_search_locations = ["/some/path"]
            mock_find_spec.return_value = mock_spec

            main()

            # Let's verify that 'torch' and other ML libraries were checked
            checked_modules = [call.args[0] for call in mock_find_spec.call_args_list]
            assert "torch" in checked_modules
            assert "sklearn" in checked_modules
            assert "llama_cpp" in checked_modules

            mock_exit.assert_not_called()
            mock_pyinstaller_main.run.assert_called_once_with(
                ["smart-autosorter.spec", "--noconfirm", "--clean"]
            )


def test_build_script_standard_missing_package():
    """Verify that a standard build fails and prints missing package if any ML package is missing."""
    mock_pyinstaller_main.run.reset_mock()
    with patch("sys.argv", ["build.py"]):
        with (
            patch("importlib.util.find_spec") as mock_find_spec,
            patch("sys.exit", side_effect=SystemExit) as mock_exit,
            patch("builtins.print") as mock_print,
        ):

            def mock_find(name):
                # Mock torch as missing, but others as present
                if name == "torch":
                    return None
                mock_spec = MagicMock()
                mock_spec.submodule_search_locations = ["/some/path"]
                return mock_spec

            mock_find_spec.side_effect = mock_find

            with pytest.raises(SystemExit):
                main()

            # It should have called sys.exit(1) due to missing PyTorch
            mock_exit.assert_called_once_with(1)
            mock_pyinstaller_main.run.assert_not_called()

            # Check if it printed the missing package name
            printed_messages = "".join(
                [call.args[0] for call in mock_print.call_args_list]
            )
            assert "PyTorch" in printed_messages
