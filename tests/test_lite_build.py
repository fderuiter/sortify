import os
import sys
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


def test_spec_file_partitioning():
    """Verify that the smart-autosorter.spec file correctly partitions dynamic libraries and static files."""
    spec_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "smart-autosorter.spec")
    assert os.path.exists(spec_path)
    
    with open(spec_path, "r", encoding="utf-8") as f:
        spec_content = f.read()
        
    # We will mock the PyInstaller classes/functions that are globally available when running a spec file.
    mock_globals = {
        "Analysis": MagicMock(),
        "PYZ": MagicMock(),
        "EXE": MagicMock(),
        "COLLECT": MagicMock(),
        "__file__": spec_path,
    }
    
    # Let's mock importlib.util.find_spec to return a custom location for sqlcipher3
    # and mock os.walk to return a mix of .so, .dll, .dylib, and standard files (.py, .pyc, .txt).
    mock_sqlcipher_dir = "/mock/sqlcipher3"
    
    mock_find_spec = MagicMock()
    mock_spec = MagicMock()
    mock_spec.submodule_search_locations = [mock_sqlcipher_dir]
    mock_find_spec.return_value = mock_spec
    
    mock_walk_data = [
        (
            "/mock/sqlcipher3",
            [],
            ["__init__.py", "_sqlite3.so", "_sqlite3.dll", "_sqlite3.dylib", "dbapi2.py"]
        ),
        (
            "/mock/sqlcipher3/sub",
            [],
            ["extra.so", "doc.txt"]
        )
    ]
    
    mock_hooks = MagicMock()
    mock_hooks.collect_all.return_value = ([], [], [])

    with patch("importlib.util.find_spec", mock_find_spec), \
         patch("os.walk", return_value=mock_walk_data), \
         patch("os.path.exists", return_value=True), \
         patch.dict(sys.modules, {
             "PyInstaller": MagicMock(),
             "PyInstaller.utils": MagicMock(),
             "PyInstaller.utils.hooks": mock_hooks,
         }):
        
        # Execute the spec file in our mock global context
        exec(spec_content, mock_globals)
        
        # Now let's inspect the `datas` and `binaries` that were passed to `Analysis`
        # Analysis is called as Analysis(...)
        analysis_call = mock_globals["Analysis"].call_args
        assert analysis_call is not None
        
        # Check kwargs
        kwargs = analysis_call.kwargs
        datas_list = kwargs.get("datas", [])
        binaries_list = kwargs.get("binaries", [])
        
        # Filter lists to find elements starting with the mock path or target destination directory 'sqlcipher3'
        sqlcipher_datas = [item for item in datas_list if "sqlcipher3" in item[1]]
        sqlcipher_binaries = [item for item in binaries_list if "sqlcipher3" in item[1]]
        
        # Expected binaries:
        # _sqlite3.so -> sqlcipher3
        # _sqlite3.dll -> sqlcipher3
        # _sqlite3.dylib -> sqlcipher3
        # extra.so -> sqlcipher3/sub
        expected_binaries = {
            (os.path.join("/mock/sqlcipher3", "_sqlite3.so"), "sqlcipher3"),
            (os.path.join("/mock/sqlcipher3", "_sqlite3.dll"), "sqlcipher3"),
            (os.path.join("/mock/sqlcipher3", "_sqlite3.dylib"), "sqlcipher3"),
            (os.path.join("/mock/sqlcipher3/sub", "extra.so"), os.path.join("sqlcipher3", "sub")),
        }
        
        # Expected datas:
        # __init__.py -> sqlcipher3
        # dbapi2.py -> sqlcipher3
        # doc.txt -> sqlcipher3/sub
        expected_datas = {
            (os.path.join("/mock/sqlcipher3", "__init__.py"), "sqlcipher3"),
            (os.path.join("/mock/sqlcipher3", "dbapi2.py"), "sqlcipher3"),
            (os.path.join("/mock/sqlcipher3/sub", "doc.txt"), os.path.join("sqlcipher3", "sub")),
        }
        
        # Convert list of tuples to set for comparison (converting paths to match OS separator)
        actual_binaries = {
            (os.path.normpath(src), os.path.normpath(dst)) for src, dst in sqlcipher_binaries
        }
        actual_datas = {
            (os.path.normpath(src), os.path.normpath(dst)) for src, dst in sqlcipher_datas
        }
        
        normalized_expected_binaries = {
            (os.path.normpath(src), os.path.normpath(dst)) for src, dst in expected_binaries
        }
        normalized_expected_datas = {
            (os.path.normpath(src), os.path.normpath(dst)) for src, dst in expected_datas
        }
        
        assert normalized_expected_binaries.issubset(actual_binaries) or normalized_expected_binaries == actual_binaries
        assert normalized_expected_datas.issubset(actual_datas) or normalized_expected_datas == actual_datas
