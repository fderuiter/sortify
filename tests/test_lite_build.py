from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings
from app.core.extractor_strategies import ImageExtractor, XlsxExtractor
from app.core.verifier import is_ml_available
from app.ui.app import AutoSorterApp


def test_is_ml_available_true():
    with patch("builtins.__import__", return_value=MagicMock()):
        assert is_ml_available() is True


def test_is_ml_available_false():
    def mock_import(name, *args, **kwargs):
        if name in ("torch", "easyocr"):
            raise ImportError(f"No module named '{name}'")
        return MagicMock()

    with patch("builtins.__import__", side_effect=mock_import):
        assert is_ml_available() is False


def test_toggle_ai_assisted_naming_with_ml():
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = True
    app = AutoSorterApp(settings)
    app.ai_naming_switch = MagicMock()
    app._rebuild_plan_async = MagicMock()

    with patch("app.core.verifier.is_ml_available", return_value=True):
        # Toggling on with ML available should enable it
        mock_event = MagicMock()
        mock_event.value = True
        app.toggle_ai_assisted_naming(mock_event)
        assert settings.AI_ASSISTED_NAMING is True
        app._rebuild_plan_async.assert_called_once()


def test_toggle_ai_assisted_naming_without_ml():
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = True
    app = AutoSorterApp(settings)
    app.ai_naming_switch = MagicMock()
    app._rebuild_plan_async = MagicMock()
    app.show_ml_warning_dialog = MagicMock()

    with patch("app.core.verifier.is_ml_available", return_value=False):
        # Toggling on with ML missing should trigger the warning dialog and revert
        mock_event = MagicMock()
        mock_event.value = True
        app.toggle_ai_assisted_naming(mock_event)
        assert settings.AI_ASSISTED_NAMING is False
        app.show_ml_warning_dialog.assert_called_once_with("AI-assisted naming")
        assert app.ai_naming_switch.value is False


def test_ocr_warning_dialog_on_scan():
    settings = AppSettings()
    app = AutoSorterApp(settings)
    app.show_ml_warning_dialog = MagicMock()
    app.progress_bar = MagicMock()
    app.status_label = MagicMock()
    app.cancel_btn = MagicMock()
    
    # Mocking files with images/pdfs and no ML
    with patch("app.core.scanner.get_files_recursively", return_value=["file1.png", "file2.pdf"]):
        with patch("app.core.verifier.is_ml_available", return_value=False):
            with patch("app.core.metadata.MetadataPass.run", return_value=[]):
                with patch("asyncio.sleep", return_value=None):
                    # Mock other methods to avoid side effects
                    app.app_session = MagicMock()
                    app.app_session.process_items = MagicMock(return_value=iter([]))
                    
                    import asyncio
                    asyncio.run(app._scan_and_process_worker())
                    app.show_ml_warning_dialog.assert_called_once_with("Visual text extraction (OCR)")


def test_xlsx_extractor_fallback():
    extractor = XlsxExtractor()
    with patch("builtins.__import__", side_effect=ImportError):
        with pytest.raises(ImportError):
            extractor.extract("dummy.xlsx")


def test_image_extractor_fallback():
    extractor = ImageExtractor()
    with patch("app.core.extractor_strategies.get_ocr_reader", return_value=None):
        with patch("PIL.Image.open", return_value=MagicMock()):
            res = extractor.extract("dummy.png")
            assert res == "[STATUS:ERROR: Vision Model Offline]"
