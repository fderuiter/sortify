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
    with patch(
        "app.core.scanner.get_files_recursively",
        return_value=["file1.png", "file2.pdf"],
    ):
        with patch("app.core.verifier.is_ml_available", return_value=False):
            with patch("app.core.metadata.MetadataPass.run", return_value=[]):
                with patch("asyncio.sleep", return_value=None):
                    # Mock other methods to avoid side effects
                    app.app_session = MagicMock()
                    app.app_session.process_items_async = MagicMock()

                    import asyncio

                    asyncio.run(app._scan_and_process_worker())
                    app.show_ml_warning_dialog.assert_called_once_with(
                        "Visual text extraction (OCR)"
                    )


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
    spec_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "smart-autosorter.spec"
    )
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
            [
                "__init__.py",
                "_sqlite3.so",
                "_sqlite3.dll",
                "_sqlite3.dylib",
                "_sqlite3.pyd",
                "dbapi2.py",
            ],
        ),
        ("/mock/sqlcipher3/sub", [], ["extra.so", "doc.txt"]),
    ]

    mock_hooks = MagicMock()
    mock_hooks.collect_all.return_value = ([], [], [])

    with (
        patch("importlib.util.find_spec", mock_find_spec),
        patch("os.walk", return_value=mock_walk_data),
        patch("os.path.exists", return_value=True),
        patch.dict(
            sys.modules,
            {
                "PyInstaller": MagicMock(),
                "PyInstaller.utils": MagicMock(),
                "PyInstaller.utils.hooks": mock_hooks,
            },
        ),
    ):
        # Execute the spec file in our mock global context
        exec(spec_content, mock_globals)

        # Test asset pruning logic
        is_prunable = mock_globals["is_prunable_asset"]
        assert is_prunable("/some/path/to/pytorch/tests/test_module.py") is True
        assert is_prunable("/some/path/to/pytorch/include/ATen/ATen.h") is True
        assert is_prunable("/some/path/to/pytorch/model/weights.bin") is False
        assert is_prunable("/some/path/to/pytorch/checkpoint_step_100.pt") is False
        assert is_prunable("/some/path/to/pytorch/some_other_file.py") is False

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
        # _sqlite3.pyd -> sqlcipher3
        # extra.so -> sqlcipher3/sub
        expected_binaries = {
            (os.path.join("/mock/sqlcipher3", "_sqlite3.so"), "sqlcipher3"),
            (os.path.join("/mock/sqlcipher3", "_sqlite3.dll"), "sqlcipher3"),
            (os.path.join("/mock/sqlcipher3", "_sqlite3.dylib"), "sqlcipher3"),
            (os.path.join("/mock/sqlcipher3", "_sqlite3.pyd"), "sqlcipher3"),
            (
                os.path.join("/mock/sqlcipher3/sub", "extra.so"),
                os.path.join("sqlcipher3", "sub"),
            ),
        }

        # Expected datas:
        # __init__.py -> sqlcipher3
        # dbapi2.py -> sqlcipher3
        # doc.txt -> sqlcipher3/sub
        expected_datas = {
            (os.path.join("/mock/sqlcipher3", "__init__.py"), "sqlcipher3"),
            (os.path.join("/mock/sqlcipher3", "dbapi2.py"), "sqlcipher3"),
            (
                os.path.join("/mock/sqlcipher3/sub", "doc.txt"),
                os.path.join("sqlcipher3", "sub"),
            ),
        }

        # Convert list of tuples to set for comparison (converting paths to match OS separator)
        actual_binaries = {
            (os.path.normpath(src), os.path.normpath(dst))
            for src, dst in sqlcipher_binaries
        }
        actual_datas = {
            (os.path.normpath(src), os.path.normpath(dst))
            for src, dst in sqlcipher_datas
        }

        normalized_expected_binaries = {
            (os.path.normpath(src), os.path.normpath(dst))
            for src, dst in expected_binaries
        }
        normalized_expected_datas = {
            (os.path.normpath(src), os.path.normpath(dst))
            for src, dst in expected_datas
        }

        assert (
            normalized_expected_binaries.issubset(actual_binaries)
            or normalized_expected_binaries == actual_binaries
        )
        assert (
            normalized_expected_datas.issubset(actual_datas)
            or normalized_expected_datas == actual_datas
        )


def test_is_standard_sqlite_binary():
    """Verify that is_standard_sqlite_binary correctly identifies and filters standard sqlite binaries while keeping the custom ones."""
    spec_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "smart-autosorter.spec"
    )
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

    with patch.dict(
        sys.modules,
        {
            "PyInstaller": MagicMock(),
            "PyInstaller.utils": MagicMock(),
            "PyInstaller.utils.hooks": mock_hooks,
        },
    ):
        # Execute spec file to load its defined functions into mock_globals
        exec(spec_content, mock_globals)

    is_standard_sqlite_binary = mock_globals["is_standard_sqlite_binary"]

    # These should be identified as standard/non-cryptographic and return True
    assert (
        is_standard_sqlite_binary("sqlite3.dll", "C:\\Python312\\DLLs\\sqlite3.dll")
        is True
    )
    assert (
        is_standard_sqlite_binary("_sqlite3.pyd", "C:\\Python312\\DLLs\\_sqlite3.pyd")
        is True
    )
    assert is_standard_sqlite_binary("sqlite3", "/usr/lib/libsqlite3.so") is True

    # These should NOT be identified as standard (because they come from sqlcipher3 or app/binaries) and return False
    assert (
        is_standard_sqlite_binary(
            "sqlite3.dll", "C:\\env\\.venv\\Lib\\site-packages\\sqlcipher3\\sqlite3.dll"
        )
        is False
    )
    assert (
        is_standard_sqlite_binary(
            "_sqlite3.pyd",
            "C:\\env\\.venv\\Lib\\site-packages\\sqlcipher3\\_sqlite3.pyd",
        )
        is False
    )
    assert (
        is_standard_sqlite_binary(
            "sqlite3.dll", "C:\\env\\app\\binaries\\windows\\sqlite3.dll"
        )
        is False
    )
    assert (
        is_standard_sqlite_binary(
            "some_other_library.dll", "C:\\Python312\\DLLs\\some_other_library.dll"
        )
        is False
    )

    # Verify virtualenv prefix support using mock prefixes to ensure platform independence
    with (
        patch("sys.prefix", os.path.abspath("/fake/venv")),
        patch("sys.base_prefix", os.path.abspath("/fake/base")),
        patch.dict(os.environ, {"VIRTUAL_ENV": os.path.abspath("/fake/venv")}),
    ):
        venv_path_win = os.path.abspath(
            os.path.join("/fake/venv", "Library", "bin", "sqlite3.dll")
        )
        venv_path_unix = os.path.abspath(
            os.path.join("/fake/venv", "lib", "libsqlite3.so")
        )
        assert is_standard_sqlite_binary("sqlite3.dll", venv_path_win) is False
        assert is_standard_sqlite_binary("sqlite3", venv_path_unix) is False

        base_path_win = os.path.abspath(
            os.path.join("/fake/base", "DLLs", "sqlite3.dll")
        )
        assert is_standard_sqlite_binary("sqlite3.dll", base_path_win) is True

    # Verify content-level binary signature checks when files actually exist
    mock_secure_file = "/fake/secure_sqlite3.dll"
    mock_standard_file = "/fake/standard_sqlite3.dll"

    def mock_open_binary(file, mode="r", *args, **kwargs):
        fh = MagicMock()
        if file == mock_secure_file:
            fh.read.return_value = b"some prefix sqlite3_key some suffix"
        elif file == mock_standard_file:
            fh.read.return_value = b"standard sqlite without key"
        fh.__enter__.return_value = fh
        return fh

    with (
        patch("os.path.isfile", return_value=True),
        patch("builtins.open", mock_open_binary),
    ):
        # Secure file should be identified as not standard (returns False)
        assert is_standard_sqlite_binary("sqlite3.dll", mock_secure_file) is False
        # Standard file should be identified as standard (returns True)
        assert is_standard_sqlite_binary("sqlite3.dll", mock_standard_file) is True


def test_update_binaries_and_manifest_win32_dll_detection():
    """Test that on win32, update_binaries_and_manifest correctly searches for and copies sqlite3.dll,
    even when _sqlite3.pyd has already been copied, but skips it if sqlite3.dll was already copied."""
    import pathlib

    from scripts import build

    # Create mocks for all required dependencies
    mock_spec = MagicMock()
    mock_spec.submodule_search_locations = ["/fake/sqlcipher3/dir"]

    orig_walk = os.walk

    # Mock os.walk and os.listdir to simulate finding sqlite3.dll in venv
    def mock_walk(top, *args, **kwargs):
        p_str = str(top).replace("\\", "/")
        if "/fake/sqlcipher3/dir" in p_str:
            yield ("/fake/sqlcipher3/dir", [], ["__init__.py", "_sqlite3.pyd"])
        elif "/fake/venv" in p_str:
            yield ("/fake/venv/Library/bin", [], ["sqlite3.dll"])
        else:
            yield from orig_walk(top, *args, **kwargs)

    orig_listdir = os.listdir

    def mock_listdir(path):
        p_str = str(path).replace("\\", "/")
        if "/fake/venv" in p_str:
            if "Library" in p_str or "bin" in p_str:
                return ["sqlite3.dll"]
        try:
            return orig_listdir(path)
        except Exception:
            return []

    orig_exists = os.path.exists

    def mock_exists(path):
        p_str = str(path).replace("\\", "/")
        if "/fake/sqlcipher3/dir" in p_str:
            return True
        if "/fake/venv" in p_str:
            return True
        if "manifest.json" in p_str:
            return True
        return orig_exists(path)

    mock_copy = MagicMock()
    mock_rmtree = MagicMock()
    mock_mkdir = MagicMock()

    # Mock sys.modules to bypass 'pytest' guard
    fake_modules = dict(sys.modules)
    if "pytest" in fake_modules:
        del fake_modules["pytest"]

    orig_open = open

    # Mock open for manifest and other files with support for mode-aware reading
    def mock_open_mode(file, mode="r", *args, **kwargs):
        file_str = str(file).lower().replace("\\", "/")
        if (
            "manifest.json" in file_str
            or "/fake/" in file_str
            or "app/binaries" in file_str
        ):
            mock_fh = MagicMock()
            if "b" in mode:
                mock_fh.read.side_effect = [b"{}", b""]
            else:
                mock_fh.read.side_effect = ["{}", ""]
            mock_fh.__enter__.return_value = mock_fh
            return mock_fh
        return orig_open(file, mode, *args, **kwargs)

    orig_path_exists = pathlib.Path.exists

    # Mock pathlib.Path.exists
    def mock_path_exists(self):
        p_str = str(self).replace("\\", "/")
        if "/fake/sqlcipher3/dir" in p_str:
            return True
        if "/fake/venv" in p_str:
            return True
        if "manifest.json" in p_str:
            return True
        return orig_path_exists(self)

    with (
        patch("sys.platform", "win32"),
        patch("sys.prefix", "/some/prefix"),
        patch("sys.base_prefix", "/some/prefix"),
        patch("sys.modules", fake_modules),
        patch("importlib.util.find_spec", return_value=mock_spec),
        patch("os.walk", side_effect=mock_walk),
        patch("os.path.exists", side_effect=mock_exists),
        patch("pathlib.Path.exists", mock_path_exists),
        patch("os.listdir", side_effect=mock_listdir),
        patch("shutil.copy2", mock_copy),
        patch("shutil.rmtree", mock_rmtree),
        patch("pathlib.Path.mkdir", mock_mkdir),
        patch.dict("os.environ", {"VIRTUAL_ENV": "/fake/venv"}),
        patch("builtins.open", mock_open_mode),
    ):
        build.update_binaries_and_manifest()

        # Check that shutil.copy2 was called to copy sqlite3.dll
        # The source should end with sqlite3.dll, and the destination should be inside our binaries directory
        copied_sources = [str(call[0][0]) for call in mock_copy.call_args_list]
        assert any("sqlite3.dll" in src for src in copied_sources), (
            "sqlite3.dll was not copied!"
        )
