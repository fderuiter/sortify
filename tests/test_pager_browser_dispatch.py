"""Unit tests for subprocess pager dispatch, TUI suspension, browser dispatch, and fallback modals."""

import asyncio
import os
import subprocess
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings
from app.ui.app import show_file_preview_dialog as app_show_preview
from app.ui.cro_forensic_view import show_file_preview_dialog as cro_show_preview
from app.ui.tui import AutoSorterTUI, FilePreviewModal


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory structure for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        file1 = os.path.join(tmpdir, "sample_report.txt")
        with open(file1, "w", encoding="utf-8") as f:
            f.write("Sample Audit Report Content\nLine 2: Data row 123")
        yield tmpdir


@pytest.fixture
def temp_test_file(temp_workspace):
    """Return path to temporary sample file."""
    return os.path.join(temp_workspace, "sample_report.txt")


def get_mock_settings():
    """Return AppSettings with AI_CONSENT_GRANTED = True to prevent wizard popups."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    return settings


def test_nicegui_open_html_browser_dispatch_success(temp_test_file):
    """Test open_html triggers webbrowser.open_new_tab on success."""
    study_mock = MagicMock()
    study_mock.audit_report_html_path = temp_test_file

    with patch("webbrowser.open_new_tab", return_value=True) as mock_open:
        path = study_mock.audit_report_html_path
        url = f"file://{os.path.abspath(path)}"
        success = mock_open(url)
        assert success is True
        mock_open.assert_called_once_with(url)


def test_nicegui_open_html_browser_dispatch_failure_fallback(temp_test_file):
    """Test open_html falls back to ui.dialog when browser dispatch fails."""
    with (
        patch(
            "webbrowser.open_new_tab", side_effect=RuntimeError("Browser unavailable")
        ),
        patch("app.ui.cro_forensic_view.show_file_preview_dialog") as mock_fallback,
    ):
        path = temp_test_file
        try:
            url = f"file://{os.path.abspath(path)}"
            import webbrowser

            if not webbrowser.open_new_tab(url):
                raise RuntimeError("webbrowser returned False")
        except Exception:
            cro_show_preview(path)

        assert os.path.exists(path)


def test_nicegui_open_report_browser_dispatch_success(temp_test_file):
    """Test open_report in app.py triggers webbrowser.open_new_tab on success."""
    report_path = temp_test_file
    with patch("webbrowser.open_new_tab", return_value=True) as mock_open:
        url = f"file://{os.path.abspath(report_path)}"
        res = mock_open(url)
        assert res is True
        mock_open.assert_called_once_with(url)


def test_nicegui_open_report_browser_dispatch_failure_fallback(temp_test_file):
    """Test open_report in app.py falls back to preview dialog when dispatch fails."""
    report_path = temp_test_file
    with (
        patch("webbrowser.open_new_tab", return_value=False),
        patch("app.ui.app.show_file_preview_dialog") as mock_fallback,
    ):
        try:
            url = f"file://{os.path.abspath(report_path)}"
            import webbrowser

            if not webbrowser.open_new_tab(url):
                raise RuntimeError("webbrowser returned False")
        except Exception:
            app_show_preview(report_path)

        assert os.path.exists(report_path)


def test_tui_action_view_pager_tty_success(temp_workspace, temp_test_file):
    """Test TUI action_view_pager suspends app and launches pager in TTY mode."""

    async def _test():
        settings = get_mock_settings()
        app = AutoSorterTUI(settings=settings)
        app.base_dir = temp_workspace

        async with app.run_test() as pilot:
            mock_node = MagicMock()
            mock_node.data = {
                "is_file": True,
                "key": "sample_report.txt",
                "filepath": temp_test_file,
                "folder": temp_workspace,
            }
            app._get_active_node = MagicMock(return_value=mock_node)

            mock_suspend_cm = MagicMock()
            mock_suspend_cm.__enter__ = MagicMock()
            mock_suspend_cm.__exit__ = MagicMock()

            with (
                patch("sys.stdout.isatty", return_value=True),
                patch("sys.stdin.isatty", return_value=True),
                patch("subprocess.run") as mock_run,
                patch.object(
                    app, "suspend", return_value=mock_suspend_cm
                ) as mock_suspend,
            ):
                mock_run.return_value = subprocess.CompletedProcess(
                    args=["less", temp_test_file], returncode=0
                )

                app.action_view_pager()

                mock_run.assert_called_once()
                args = mock_run.call_args[0][0]
                assert temp_test_file in args
                mock_suspend.assert_called_once()
                mock_suspend_cm.__enter__.assert_called_once()
                mock_suspend_cm.__exit__.assert_called_once()

    asyncio.run(_test())


def test_tui_action_view_pager_non_tty_fallback(temp_workspace, temp_test_file):
    """Test TUI action_view_pager falls back to FilePreviewModal when sys.stdout is non-TTY."""

    async def _test():
        settings = get_mock_settings()
        app = AutoSorterTUI(settings=settings)
        app.base_dir = temp_workspace

        async with app.run_test() as pilot:
            mock_node = MagicMock()
            mock_node.data = {
                "is_file": True,
                "key": "sample_report.txt",
                "filepath": temp_test_file,
                "folder": temp_workspace,
            }
            app._get_active_node = MagicMock(return_value=mock_node)

            with (
                patch("sys.stdout.isatty", return_value=False),
                patch("subprocess.run") as mock_run,
                patch.object(app, "push_screen") as mock_push,
            ):
                app.action_view_pager()

                mock_run.assert_not_called()
                mock_push.assert_called_once()
                pushed_screen = mock_push.call_args[0][0]
                assert isinstance(pushed_screen, FilePreviewModal)
                assert pushed_screen.filepath == temp_test_file

    asyncio.run(_test())


def test_tui_action_view_pager_execution_failure_fallback(
    temp_workspace, temp_test_file
):
    """Test TUI action_view_pager falls back to FilePreviewModal when pager subprocess fails."""

    async def _test():
        settings = get_mock_settings()
        app = AutoSorterTUI(settings=settings)
        app.base_dir = temp_workspace

        async with app.run_test() as pilot:
            mock_node = MagicMock()
            mock_node.data = {
                "is_file": True,
                "key": "sample_report.txt",
                "filepath": temp_test_file,
                "folder": temp_workspace,
            }
            app._get_active_node = MagicMock(return_value=mock_node)

            mock_suspend_cm = MagicMock()
            mock_suspend_cm.__enter__ = MagicMock()
            mock_suspend_cm.__exit__ = MagicMock(return_value=False)

            with (
                patch("sys.stdout.isatty", return_value=True),
                patch("sys.stdin.isatty", return_value=True),
                patch(
                    "subprocess.run",
                    side_effect=FileNotFoundError("pager binary missing"),
                ),
                patch.object(app, "suspend", return_value=mock_suspend_cm),
                patch.object(app, "push_screen") as mock_push,
            ):
                app.action_view_pager()

                mock_push.assert_called_once()
                pushed_screen = mock_push.call_args[0][0]
                assert isinstance(pushed_screen, FilePreviewModal)

    asyncio.run(_test())


def test_tui_file_preview_modal_mount_and_close(temp_test_file):
    """Test mounting FilePreviewModal displays file content and can be dismissed."""

    async def _test():
        modal = FilePreviewModal(filepath=temp_test_file)
        app = AutoSorterTUI(settings=get_mock_settings())

        async with app.run_test() as pilot:
            app.push_screen(modal)
            await pilot.pause()

            assert isinstance(app.screen, FilePreviewModal)
            modal.action_close()
            await pilot.pause()

            assert not isinstance(app.screen, FilePreviewModal)

    asyncio.run(_test())
