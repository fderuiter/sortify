import os
import tempfile
from unittest import mock

import pytest

from app.ui.report_helper import (
    _SERVED_ROUTES,
    register_static_route_for_file,
    serve_or_download_report,
    show_file_error_dialog,
)


@pytest.fixture(autouse=True)
def clear_route_cache():
    """Clear served route cache before each test."""
    _SERVED_ROUTES.clear()


def test_serve_or_download_report_valid_file_download():
    """Test downloading a valid report file via ui.download()."""
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        f.write(b"<html>Audit Report</html>")
        f_path = f.name

    try:
        mock_ui = mock.MagicMock()
        mock_ui.download = mock.MagicMock()

        with mock.patch("app.ui.report_helper.ui", mock_ui):
            res = serve_or_download_report(f_path, open_in_new_tab=False)
            assert res is True
            mock_ui.download.assert_called_once()
            args, kwargs = mock_ui.download.call_args
            assert os.path.realpath(f_path) in args or os.path.realpath(f_path) == args[0]
            assert kwargs.get("filename") == os.path.basename(f_path)
    finally:
        if os.path.exists(f_path):
            os.remove(f_path)


def test_serve_or_download_report_open_in_new_tab():
    """Test serving a report file via dynamic static route and open in new tab navigation."""
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        f.write(b"<html>Audit Dossier</html>")
        f_path = f.name

    try:
        mock_ui = mock.MagicMock()
        mock_ui.navigate.to = mock.MagicMock()
        mock_app = mock.MagicMock()
        mock_app.add_static_file = mock.MagicMock()

        with (
            mock.patch("app.ui.report_helper.ui", mock_ui),
            mock.patch("app.ui.report_helper.app", mock_app),
        ):
            res = serve_or_download_report(f_path, open_in_new_tab=True)
            assert res is True
            mock_app.add_static_file.assert_called_once()
            mock_ui.navigate.to.assert_called_once()
            nav_args, nav_kwargs = mock_ui.navigate.to.call_args
            assert nav_kwargs.get("new_tab") is True
            assert "/served_reports/" in nav_args[0]
    finally:
        if os.path.exists(f_path):
            os.remove(f_path)


def test_serve_or_download_report_missing_file_fallback_dialog():
    """Test that missing files trigger the fallback error modal dialog."""
    missing_path = "/tmp/non_existent_audit_dossier_12345.html"

    with mock.patch("app.ui.report_helper.show_file_error_dialog") as mock_dialog:
        res = serve_or_download_report(missing_path)
        assert res is False
        mock_dialog.assert_called_once()
        args, _ = mock_dialog.call_args
        assert missing_path in args[0] or os.path.abspath(missing_path) in args[0]


def test_serve_or_download_report_none_or_empty_path():
    """Test handling of None or empty path input."""
    with mock.patch("app.ui.report_helper.show_file_error_dialog") as mock_dialog:
        res1 = serve_or_download_report(None)
        assert res1 is False
        assert mock_dialog.call_count == 1

        res2 = serve_or_download_report("")
        assert res2 is False
        assert mock_dialog.call_count == 2


def test_serve_or_download_report_unreadable_file():
    """Test fallback modal when file reading raises PermissionError/OSError."""
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        f.write(b"<html>Secret Data</html>")
        f_path = f.name

    try:
        with (
            mock.patch("builtins.open", side_effect=PermissionError("Access denied")),
            mock.patch("app.ui.report_helper.show_file_error_dialog") as mock_dialog,
        ):
            res = serve_or_download_report(f_path)
            assert res is False
            mock_dialog.assert_called_once()
            assert "Access denied" in mock_dialog.call_args[0][1] or "Cannot read" in mock_dialog.call_args[0][1]
    finally:
        if os.path.exists(f_path):
            os.remove(f_path)


def test_show_file_error_dialog_structure():
    """Test creation and opening of fallback error modal dialog."""
    mock_dialog_obj = mock.MagicMock()
    mock_card_obj = mock.MagicMock()

    mock_ui = mock.MagicMock()
    mock_ui.dialog.return_value.__enter__.return_value = mock_dialog_obj
    mock_ui.card.return_value.__enter__.return_value = mock_card_obj

    dummy_path = "/var/log/audit/report.html"
    with mock.patch("app.ui.report_helper.ui", mock_ui):
        show_file_error_dialog(dummy_path, "Test error message")
        mock_dialog_obj.open.assert_called_once()


def test_cro_forensic_view_report_actions():
    """Test report actions in CROForensicView do not invoke webbrowser.open."""
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        f.write(b"<html>CRO Dossier</html>")
        report_path = f.name

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        f.write(b'{"manifest": true}')
        manifest_path = f.name

    try:
        import app.ui.cro_forensic_view as cro_mod

        mock_settings = mock.MagicMock()
        view = cro_mod.CROForensicView(mock_settings)

        # Mock result and study objects
        mock_study = mock.MagicMock()
        mock_study.study_id = "STUDY-001"
        mock_study.total_documents = 10
        mock_study.audit_readiness_status = "Ready"
        mock_study.compliance_score_percent = 95
        mock_study.audit_report_html_path = report_path

        mock_result = mock.MagicMock()
        mock_result.studies = [mock_study]
        mock_result.chain_of_custody_manifest_path = manifest_path

        with (
            mock.patch("webbrowser.open", side_effect=AssertionError("webbrowser.open should not be called")),
            mock.patch.object(cro_mod, "serve_or_download_report") as mock_serve,
        ):
            def open_html_func():
                return cro_mod.serve_or_download_report(
                    mock_study.audit_report_html_path, open_in_new_tab=True
                )

            open_html_func()
            mock_serve.assert_called_with(report_path, open_in_new_tab=True)

            def open_manifest_func():
                return cro_mod.serve_or_download_report(
                    mock_result.chain_of_custody_manifest_path, open_in_new_tab=False
                )

            open_manifest_func()
            mock_serve.assert_called_with(manifest_path, open_in_new_tab=False)
    finally:
        for p in (report_path, manifest_path):
            if os.path.exists(p):
                os.remove(p)


def test_register_static_route_for_file_cache():
    """Test register_static_route_for_file route caching."""
    mock_app = mock.MagicMock()
    mock_app.add_static_file = mock.MagicMock()

    with mock.patch("app.ui.report_helper.app", mock_app):
        route1 = register_static_route_for_file("/tmp/test_route_file.html")
        route2 = register_static_route_for_file("/tmp/test_route_file.html")
        assert route1 == route2
        assert mock_app.add_static_file.call_count == 1


def test_app_open_report_action():
    """Test open_report in app.py uses serve_or_download_report and does not invoke webbrowser.open."""
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        f.write(b"<html>App Dossier</html>")
        report_path = f.name

    try:
        import app.ui.app as app_mod

        with (
            mock.patch("webbrowser.open", side_effect=AssertionError("webbrowser.open should not be called")),
            mock.patch.object(app_mod, "serve_or_download_report") as mock_serve,
        ):
            def open_report_func():
                return app_mod.serve_or_download_report(report_path, open_in_new_tab=True)

            open_report_func()
            mock_serve.assert_called_with(report_path, open_in_new_tab=True)
    finally:
        if os.path.exists(report_path):
            os.remove(report_path)


def test_headless_displayless_environment():
    """Verify report downloading operates cleanly when DISPLAY/WAYLAND_DISPLAY are unset."""
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        f.write(b"<html>Headless Report</html>")
        f_path = f.name

    try:
        mock_ui = mock.MagicMock()
        mock_ui.download = mock.MagicMock()

        env_without_display = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}

        with (
            mock.patch.dict(os.environ, env_without_display, clear=True),
            mock.patch("webbrowser.open", side_effect=RuntimeError("webbrowser.open forbidden")),
            mock.patch("app.ui.report_helper.ui", mock_ui),
        ):
            res = serve_or_download_report(f_path)
            assert res is True
            mock_ui.download.assert_called_once()
    finally:
        if os.path.exists(f_path):
            os.remove(f_path)
