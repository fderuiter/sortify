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
            patch("scripts.build.download_and_prepare_weights") as mock_download,
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
            mock_download.assert_called_once()
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


def test_build_script_cpu_profile_success():
    """Verify that a CPU build passes if PyTorch is indeed CPU-only."""
    import os
    mock_pyinstaller_main.run.reset_mock()
    with patch("sys.argv", ["build.py", "--cpu"]):
        with (
            patch("importlib.util.find_spec") as mock_find_spec,
            patch("sys.exit", side_effect=SystemExit) as mock_exit,
            patch("scripts.build.download_and_prepare_weights") as mock_download,
            patch("scripts.build.update_binaries_and_manifest") as mock_update_bin,
            patch("os.path.exists", return_value=True),
            patch("os.walk", return_value=[("dist/smart-autosorter", [], [])]), # No GPU binaries
        ):
            # Setup sqlcipher3 and torch mock
            mock_spec = MagicMock()
            mock_spec.submodule_search_locations = ["/some/path"]
            mock_find_spec.return_value = mock_spec

            # Mock torch module
            mock_torch = MagicMock()
            if sys.platform in ("win32", "linux"):
                mock_torch.__version__ = "2.1.2+cpu"
            else: # darwin
                mock_torch.__version__ = "2.1.2"
                mock_torch.cuda.is_available.return_value = False

            with patch.dict("sys.modules", {"torch": mock_torch}):
                main()

                # Verify that CPU_BUILD env var is set
                assert os.environ.get("CPU_BUILD") == "1"

                mock_exit.assert_not_called()
                mock_download.assert_called_once()
                mock_pyinstaller_main.run.assert_called_once_with(
                    ["smart-autosorter.spec", "--noconfirm", "--clean"]
                )


def test_build_script_cpu_profile_failure_cuda():
    """Verify that a CPU build fails if PyTorch is CUDA-enabled."""
    mock_pyinstaller_main.run.reset_mock()
    with patch("sys.argv", ["build.py", "--cpu"]):
        with (
            patch("importlib.util.find_spec") as mock_find_spec,
            patch("sys.exit", side_effect=SystemExit) as mock_exit,
            patch("builtins.print") as mock_print,
        ):
            # Setup sqlcipher3 and torch mock
            mock_spec = MagicMock()
            mock_spec.submodule_search_locations = ["/some/path"]
            mock_find_spec.return_value = mock_spec

            # Mock torch module
            mock_torch = MagicMock()
            if sys.platform in ("win32", "linux"):
                mock_torch.__version__ = "2.1.2+cu121"
            else: # darwin
                mock_torch.__version__ = "2.1.2"
                mock_torch.cuda.is_available.return_value = True

            with patch.dict("sys.modules", {"torch": mock_torch}):
                with pytest.raises(SystemExit):
                    main()

                mock_exit.assert_called_once_with(1)
                # Check printed error messages
                printed_messages = "".join(
                    [call.args[0] for call in mock_print.call_args_list if call.args]
                )
                assert "Non-CPU PyTorch detected" in printed_messages


def test_build_script_cpu_profile_scan_fails_if_gpu_binaries_exist():
    """Verify that post-build scan fails if GPU binaries are found."""
    mock_pyinstaller_main.run.reset_mock()
    with patch("sys.argv", ["build.py", "--cpu"]):
        with (
            patch("importlib.util.find_spec") as mock_find_spec,
            patch("sys.exit", side_effect=SystemExit) as mock_exit,
            patch("scripts.build.download_and_prepare_weights") as mock_download,
            patch("scripts.build.update_binaries_and_manifest") as mock_update_bin,
            patch("os.path.exists", return_value=True),
            patch("os.walk", return_value=[("dist/smart-autosorter", [], ["libcudart.so"])]),
            patch("builtins.print") as mock_print,
        ):
            # Setup sqlcipher3 and torch mock
            mock_spec = MagicMock()
            mock_spec.submodule_search_locations = ["/some/path"]
            mock_find_spec.return_value = mock_spec

            # Mock torch module
            mock_torch = MagicMock()
            if sys.platform in ("win32", "linux"):
                mock_torch.__version__ = "2.1.2+cpu"
            else: # darwin
                mock_torch.__version__ = "2.1.2"
                mock_torch.cuda.is_available.return_value = False

            with patch.dict("sys.modules", {"torch": mock_torch}):
                with pytest.raises(SystemExit):
                    main()

                mock_exit.assert_called_once_with(1)
                printed_messages = "".join(
                    [call.args[0] for call in mock_print.call_args_list if call.args]
                )
                assert "Standalone bundle contains GPU/CUDA/cuDNN binaries" in printed_messages

