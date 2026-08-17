import sys
from unittest.mock import MagicMock, patch

import pytest

from scripts.prepare_offline import main


@patch("scripts.prepare_offline.subprocess.run")
@patch("scripts.prepare_offline.shutil.rmtree")
@patch("scripts.prepare_offline.shutil.make_archive")
@patch("scripts.prepare_offline.Path.mkdir")
@patch("builtins.print")
def test_prepare_offline_cpu_success(
    mock_print, mock_mkdir, mock_archive, mock_rmtree, mock_run
):
    """Test that prepare_offline.py succeeds if only CPU wheels are compiled/downloaded."""
    with patch("sys.argv", ["prepare_offline.py", "--cpu"]):
        with patch("sys.exit", side_effect=SystemExit) as mock_exit:
            mock_requirements = (
                "torch==2.1.2+cpu\ntorchvision==0.16.2+cpu"
                if sys.platform in ("win32", "linux")
                else "torch==2.1.2"
            )

            with (
                patch("pathlib.Path.exists", return_value=True),
                patch("builtins.open", create=True) as mock_open,
                patch("pathlib.Path.glob") as mock_glob,
            ):
                # Set up open mock to return the mocked requirements string
                mock_file = MagicMock()
                mock_file.__enter__.return_value.read.return_value = mock_requirements
                mock_open.return_value = mock_file

                # Set up glob mock to return CPU wheels
                mock_wheel = MagicMock()
                mock_wheel.name = (
                    "torch-2.1.2+cpu-cp310-cp310-linux_x86_64.whl"
                    if sys.platform in ("win32", "linux")
                    else "torch-2.1.2-cp310-cp310-macosx_11_0_arm64.whl"
                )
                mock_glob.return_value = [mock_wheel]

                main()

                mock_exit.assert_not_called()
                mock_archive.assert_called_once_with(
                    "offline_bundle", "zip", "offline_bundle"
                )


@patch("scripts.prepare_offline.subprocess.run")
@patch("scripts.prepare_offline.shutil.rmtree")
@patch("scripts.prepare_offline.shutil.make_archive")
@patch("scripts.prepare_offline.Path.mkdir")
@patch("builtins.print")
def test_prepare_offline_cpu_fails_on_cuda_req(
    mock_print, mock_mkdir, mock_archive, mock_rmtree, mock_run
):
    """Test that prepare_offline.py fails if standard or CUDA PyTorch is found in requirements."""
    with patch("sys.argv", ["prepare_offline.py", "--cpu"]):
        with patch("sys.exit", side_effect=SystemExit) as mock_exit:
            # Force a CUDA-enabled torch requirement
            mock_requirements = "torch==2.1.2+cu121"

            with (
                patch("pathlib.Path.exists", return_value=True),
                patch("builtins.open", create=True) as mock_open,
                patch("pathlib.Path.glob") as mock_glob,
            ):
                mock_file = MagicMock()
                mock_file.__enter__.return_value.read.return_value = mock_requirements
                mock_open.return_value = mock_file
                mock_glob.return_value = []

                with pytest.raises(SystemExit):
                    main()

                mock_exit.assert_called_once_with(1)


@patch("scripts.prepare_offline.subprocess.run")
@patch("scripts.prepare_offline.shutil.rmtree")
@patch("scripts.prepare_offline.shutil.make_archive")
@patch("scripts.prepare_offline.Path.mkdir")
@patch("builtins.print")
def test_prepare_offline_cpu_fails_on_cuda_wheel(
    mock_print, mock_mkdir, mock_archive, mock_rmtree, mock_run
):
    """Test that prepare_offline.py fails if a CUDA-enabled wheel is downloaded."""
    with patch("sys.argv", ["prepare_offline.py", "--cpu"]):
        with patch("sys.exit", side_effect=SystemExit) as mock_exit:
            mock_requirements = (
                "torch==2.1.2+cpu"
                if sys.platform in ("win32", "linux")
                else "torch==2.1.2"
            )

            with (
                patch("pathlib.Path.exists", return_value=True),
                patch("builtins.open", create=True) as mock_open,
                patch("pathlib.Path.glob") as mock_glob,
            ):
                mock_file = MagicMock()
                mock_file.__enter__.return_value.read.return_value = mock_requirements
                mock_open.return_value = mock_file

                # Non-CPU wheel name
                mock_wheel = MagicMock()
                mock_wheel.name = "torch-2.1.2+cu121-cp310-cp310-linux_x86_64.whl"
                mock_glob.return_value = [mock_wheel]

                with pytest.raises(SystemExit):
                    main()

                mock_exit.assert_called_once_with(1)


@patch("scripts.prepare_offline.subprocess.run")
@patch("scripts.prepare_offline.shutil.rmtree")
@patch("scripts.prepare_offline.shutil.make_archive")
@patch("scripts.prepare_offline.Path.mkdir")
@patch("builtins.print")
def test_prepare_offline_preserves_models(
    mock_print, mock_mkdir, mock_archive, mock_rmtree, mock_run
):
    """Test that prepare_offline.py does not delete the model weight subdirectories."""
    with patch("sys.argv", ["prepare_offline.py", "--cpu"]):
        with patch("sys.exit", side_effect=SystemExit) as mock_exit:
            mock_requirements = (
                "torch==2.1.2+cpu\ntorchvision==0.16.2+cpu"
                if sys.platform in ("win32", "linux")
                else "torch==2.1.2"
            )

            # We want to check that Path("offline_bundle") is not removed.
            # Only Path("offline_bundle/wheels") should be removed if it exists.
            deleted_paths = []
            def fake_rmtree(path, *args, **kwargs):
                deleted_paths.append(str(path))

            mock_rmtree.side_effect = fake_rmtree

            with (
                patch("pathlib.Path.exists", return_value=True),
                patch("builtins.open", create=True) as mock_open,
                patch("pathlib.Path.glob") as mock_glob,
            ):
                mock_file = MagicMock()
                mock_file.__enter__.return_value.read.return_value = mock_requirements
                mock_open.return_value = mock_file

                mock_wheel = MagicMock()
                mock_wheel.name = (
                    "torch-2.1.2+cpu-cp310-cp310-linux_x86_64.whl"
                    if sys.platform in ("win32", "linux")
                    else "torch-2.1.2-cp310-cp310-macosx_11_0_arm64.whl"
                )
                mock_glob.return_value = [mock_wheel]

                main()

                mock_exit.assert_not_called()
                # Verify that offline_bundle directory itself was not deleted
                assert "offline_bundle" not in deleted_paths
                # Verify that previous ZIP output was checked and wheels dir specifically deleted
                assert "offline_bundle/wheels" in deleted_paths or any("wheels" in p for p in deleted_paths)
