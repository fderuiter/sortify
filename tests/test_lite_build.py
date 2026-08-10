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
    mock_sqlcipher_dir = os.path.abspath("/mock/sqlcipher3")

    mock_find_spec = MagicMock()
    mock_spec = MagicMock()
    mock_spec.submodule_search_locations = [mock_sqlcipher_dir]
    mock_find_spec.return_value = mock_spec

    def mock_walk_spec(top, *args, **kwargs):
        top_abs = os.path.abspath(top)
        top_str = top_abs.lower().replace("\\", "/")
        if "sqlcipher3" in top_str:
            return [
                (
                    os.path.abspath("/mock/sqlcipher3"),
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
                (os.path.abspath("/mock/sqlcipher3/sub"), [], ["extra.so", "doc.txt"]),
            ]
        elif "app/binaries" in top_str or "app_binaries" in top_str:
            mock_sub = os.path.abspath(os.path.join(top, "windows", "sqlcipher3"))
            return [
                (
                    mock_sub,
                    [],
                    ["sqlite3.dll"],
                )
            ]
        else:
            return []

    mock_hooks = MagicMock()
    mock_hooks.collect_all.return_value = ([], [], [])

    real_exists = os.path.exists
    def mock_exists(path):
        if "binaries" in str(path).lower():
            return True
        try:
            return real_exists(path)
        except Exception:
            return False

    with (
        patch("importlib.util.find_spec", mock_find_spec),
        patch("os.walk", side_effect=mock_walk_spec),
        patch("os.path.exists", side_effect=mock_exists),
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
            (os.path.join(mock_sqlcipher_dir, "_sqlite3.so"), "sqlcipher3"),
            (os.path.join(mock_sqlcipher_dir, "_sqlite3.dll"), "sqlcipher3"),
            (os.path.join(mock_sqlcipher_dir, "_sqlite3.dylib"), "sqlcipher3"),
            (os.path.join(mock_sqlcipher_dir, "_sqlite3.pyd"), "sqlcipher3"),
            (
                os.path.join(os.path.join(mock_sqlcipher_dir, "sub"), "extra.so"),
                os.path.join("sqlcipher3", "sub"),
            ),
        }

        # Expected datas:
        # __init__.py -> sqlcipher3
        # dbapi2.py -> sqlcipher3
        # doc.txt -> sqlcipher3/sub
        expected_datas = {
            (os.path.join(mock_sqlcipher_dir, "__init__.py"), "sqlcipher3"),
            (os.path.join(mock_sqlcipher_dir, "dbapi2.py"), "sqlcipher3"),
            (
                os.path.join(os.path.join(mock_sqlcipher_dir, "sub"), "doc.txt"),
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

    # Verify virtualenv prefix support using mock prefixes to ensure platform independence
    fake_venv = os.path.abspath("fake_venv")
    fake_base = os.path.abspath("fake_base")
    with (
        patch("sys.prefix", fake_venv),
        patch("sys.base_prefix", fake_base),
        patch.dict(os.environ, {"VIRTUAL_ENV": fake_venv}),
    ):
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

        venv_path_win = os.path.join(fake_venv, "Library", "bin", "sqlite3.dll")
        venv_path_unix = os.path.join(fake_venv, "lib", "libsqlite3.so")
        assert is_standard_sqlite_binary("sqlite3.dll", venv_path_win) is False
        assert is_standard_sqlite_binary("sqlite3", venv_path_unix) is False

        base_path_win = os.path.join(fake_base, "DLLs", "sqlite3.dll")
        assert is_standard_sqlite_binary("sqlite3.dll", base_path_win) is True


def test_update_binaries_and_manifest_win32_dll_detection(tmp_path):
    """Test that on win32, update_binaries_and_manifest correctly searches for and copies sqlite3.dll,
    even when _sqlite3.pyd has already been copied, but skips it if sqlite3.dll was already copied."""
    import json
    import os
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from scripts import build

    # Create dummy sqlcipher3 directory
    fake_sqlcipher_dir = tmp_path / "fake_sqlcipher_dir"
    fake_sqlcipher_dir.mkdir(parents=True, exist_ok=True)
    (fake_sqlcipher_dir / "__init__.py").write_text("print('mock')", encoding="utf-8")
    (fake_sqlcipher_dir / "_sqlite3.pyd").write_bytes(b"mock_pyd_content")

    # Create dummy venv directory with Library/bin/sqlite3.dll and all other required DLL patterns to prevent Windows fallback system-wide searches
    fake_venv_dir = tmp_path / "fake_venv_dir"
    fake_venv_lib_bin = fake_venv_dir / "Library" / "bin"
    fake_venv_lib_bin.mkdir(parents=True, exist_ok=True)
    (fake_venv_lib_bin / "sqlite3.dll").write_bytes(b"mock_dll_content")
    (fake_venv_lib_bin / "libcrypto.dll").write_bytes(b"mock_crypto_content")
    (fake_venv_lib_bin / "libssl.dll").write_bytes(b"mock_ssl_content")
    (fake_venv_lib_bin / "sqlcipher.dll").write_bytes(b"mock_sqlcipher_content")
    (fake_venv_lib_bin / "libsqlcipher.dll").write_bytes(b"mock_libsqlcipher_content")

    # Create mock spec for find_spec
    mock_spec = MagicMock()
    mock_spec.submodule_search_locations = [str(fake_sqlcipher_dir)]

    # Change current working directory to tmp_path so hardcoded 'app/binaries' is relative to tmp_path
    original_cwd = os.getcwd()
    os.chdir(tmp_path)

    try:
        with (
            patch("sys.prefix", str(fake_venv_dir)),
            patch("sys.base_prefix", str(fake_venv_dir)),
            patch("importlib.util.find_spec", return_value=mock_spec),
            patch.dict("os.environ", {"VIRTUAL_ENV": str(fake_venv_dir)}),
        ):
            build.update_binaries_and_manifest(
                system_platform="win32", bypass_pytest_check=True
            )

            # Check that files were copied to the target directory
            target_dir = Path("app") / "binaries" / "windows" / "sqlcipher3"
            assert (target_dir / "__init__.py").exists()
            assert (target_dir / "_sqlite3.pyd").exists()
            assert (target_dir / "sqlite3.dll").exists()

            # Check that manifest.json was created and updated
            manifest_path = Path("app") / "binaries" / "manifest.json"
            assert manifest_path.exists()
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            assert "windows" in manifest
            assert "sqlcipher3/__init__.py" in manifest["windows"]
            assert "sqlcipher3/_sqlite3.pyd" in manifest["windows"]
            assert "sqlcipher3/sqlite3.dll" in manifest["windows"]
    finally:
        os.chdir(original_cwd)
