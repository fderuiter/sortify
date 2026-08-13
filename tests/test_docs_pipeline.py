import os
import sys
import pytest
from unittest.mock import patch, MagicMock

from scripts.generate_docs import main


def test_main_strict_flag():
    """Verify that --strict (default) and --no-strict set mkdocs arguments correctly."""
    with patch("sys.argv", ["generate_docs.py", "--no-strict"]):
        with (
            patch("scripts.generate_docs.generate_api_docs") as mock_api,
            patch("scripts.generate_docs.generate_ui_docs") as mock_ui,
            patch("scripts.generate_docs.generate_admin_guide") as mock_admin,
            patch("scripts.generate_docs.update_security_md") as mock_sec,
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            main()

            # Verify generators are called
            mock_api.assert_called_once()
            mock_ui.assert_called_once()
            mock_admin.assert_called_once()
            mock_sec.assert_called_once()

            # Verify build command does NOT have --strict
            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            cmd = args[0]
            assert "--strict" not in cmd


def test_main_default_strict():
    """Verify that by default mkdocs is run with --strict."""
    with patch("sys.argv", ["generate_docs.py"]):
        with (
            patch("scripts.generate_docs.generate_api_docs") as mock_api,
            patch("scripts.generate_docs.generate_ui_docs") as mock_ui,
            patch("scripts.generate_docs.generate_admin_guide") as mock_admin,
            patch("scripts.generate_docs.update_security_md") as mock_sec,
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            main()

            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            cmd = args[0]
            assert "--strict" in cmd


def test_main_detects_unsynced_files_on_check():
    """Verify that --check detects modified files and exits with code 1."""
    with patch("sys.argv", ["generate_docs.py", "--check"]):
        with (
            patch("scripts.generate_docs.generate_api_docs"),
            patch("scripts.generate_docs.generate_ui_docs"),
            patch("scripts.generate_docs.generate_admin_guide"),
            patch("scripts.generate_docs.update_security_md"),
            patch("subprocess.run") as mock_run,
            patch("os.path.exists", return_value=True),
            patch("builtins.open") as mock_open,
            patch("sys.exit") as mock_exit,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            # Mock read contents to change before/after
            # We have 4 files, we read them initially, and then read them again after generation.
            # Let's make the second file return different content on second read (after generation)
            file_contents = {
                os.path.join("docs", "api_reference.md"): ["content1", "content1"],
                os.path.join("docs", "ui.md"): ["content2", "different_content2"],
                os.path.join("docs", "admin_guide.md"): ["content3", "content3"],
                "SECURITY.md": ["content4", "content4"],
            }

            counters = {k: 0 for k in file_contents}

            class MockFile:
                def __init__(self, filepath):
                    self.filepath = filepath

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc_val, exc_tb):
                    pass

                def read(self):
                    # Return next item in list
                    idx = counters[self.filepath]
                    counters[self.filepath] = min(idx + 1, len(file_contents[self.filepath]) - 1)
                    return file_contents[self.filepath][idx]

            mock_open.side_effect = lambda filepath, *args, **kwargs: MockFile(filepath)

            main()

            # Verify exit was called with 1 because ui.md changed
            mock_exit.assert_called_once_with(1)


def test_main_clean_on_check():
    """Verify that --check exits cleanly with 0 if no files were changed."""
    with patch("sys.argv", ["generate_docs.py", "--check"]):
        with (
            patch("scripts.generate_docs.generate_api_docs"),
            patch("scripts.generate_docs.generate_ui_docs"),
            patch("scripts.generate_docs.generate_admin_guide"),
            patch("scripts.generate_docs.update_security_md"),
            patch("subprocess.run") as mock_run,
            patch("os.path.exists", return_value=True),
            patch("builtins.open") as mock_open,
            patch("sys.exit") as mock_exit,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            # Mock read contents to be the same before and after
            file_contents = {
                os.path.join("docs", "api_reference.md"): ["content1", "content1"],
                os.path.join("docs", "ui.md"): ["content2", "content2"],
                os.path.join("docs", "admin_guide.md"): ["content3", "content3"],
                "SECURITY.md": ["content4", "content4"],
            }

            counters = {k: 0 for k in file_contents}

            class MockFile:
                def __init__(self, filepath):
                    self.filepath = filepath

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc_val, exc_tb):
                    pass

                def read(self):
                    idx = counters[self.filepath]
                    counters[self.filepath] = min(idx + 1, len(file_contents[self.filepath]) - 1)
                    return file_contents[self.filepath][idx]

            mock_open.side_effect = lambda filepath, *args, **kwargs: MockFile(filepath)

            main()

            # sys.exit should not be called (clean exit)
            mock_exit.assert_not_called()
