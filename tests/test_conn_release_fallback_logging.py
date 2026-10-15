import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.main import run_smoke_test, write_smoke_test_error


def test_run_smoke_test_closes_and_clears_cache_on_success():
    """Verify that a successful smoke test explicitly closes active connections and clears the cache before deleting temp_dir."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    # Mock row return for selectivity and cipher_version
    mock_cursor.fetchone.side_effect = [
        ("SuperSecretData",),  # select row
        ("4.5.1",),  # cipher_version
    ]
    mock_conn.cursor.return_value = mock_cursor

    # Use a list to record the order of important operations
    call_order = []

    def mock_close():
        call_order.append("conn_close")

    mock_conn.close.side_effect = mock_close

    # Patches
    with (
        patch("tempfile.mkdtemp", return_value="/mock/temp/dir") as mock_mkdtemp,
        patch(
            "app.core.db_conn.get_db_connection", return_value=mock_conn
        ) as mock_get_conn,
        patch("app.core.db_conn.clear_connection_cache") as mock_clear_cache,
        patch("shutil.rmtree") as mock_rmtree,
    ):
        mock_clear_cache.side_effect = lambda *args, **kwargs: call_order.append(
            "clear_cache"
        )
        mock_rmtree.side_effect = lambda path: call_order.append(f"rmtree_{path}")

        # Since run_smoke_test calls sys.exit(0) on success, we catch SystemExit
        with pytest.raises(SystemExit) as excinfo:
            run_smoke_test()

        assert excinfo.value.code == 0

        # Verify call ordering
        assert "conn_close" in call_order
        assert "clear_cache" in call_order
        assert "rmtree_/mock/temp/dir" in call_order

        # Confirm clearing cache and closing connection happened before deletion
        idx_close = call_order.index("conn_close")
        idx_clear = call_order.index("clear_cache")
        idx_rmtree = call_order.index("rmtree_/mock/temp/dir")

        assert idx_close < idx_rmtree
        assert idx_clear < idx_rmtree


def test_run_smoke_test_closes_and_clears_cache_on_failure():
    """Verify that a failed smoke test still explicitly closes connection and clears cache before deleting temp_dir."""
    # Force get_db_connection to fail
    call_order = []

    with (
        patch("tempfile.mkdtemp", return_value="/mock/temp/dir_failed"),
        patch(
            "app.core.db_conn.get_db_connection", side_effect=RuntimeError("DB Failed")
        ),
        patch("app.core.db_conn.clear_connection_cache") as mock_clear_cache,
        patch("shutil.rmtree") as mock_rmtree,
        patch("app.main.write_smoke_test_error") as mock_write_error,
    ):
        mock_clear_cache.side_effect = lambda *args, **kwargs: call_order.append(
            "clear_cache"
        )
        mock_rmtree.side_effect = lambda path: call_order.append(f"rmtree_{path}")

        # Since run_smoke_test calls sys.exit(1) on failure, we catch SystemExit
        with pytest.raises(SystemExit) as excinfo:
            run_smoke_test()

        assert excinfo.value.code == 1
        assert "clear_cache" in call_order
        assert "rmtree_/mock/temp/dir_failed" in call_order

        # Check order
        idx_clear = call_order.index("clear_cache")
        idx_rmtree = call_order.index("rmtree_/mock/temp/dir_failed")
        assert idx_clear < idx_rmtree


def test_write_smoke_test_error_primary_blocked_falls_back_to_home():
    """Verify that if primary paths are read-only / blocked, it falls back to the user home configuration directory."""
    # Create a custom open implementation to block primary path (smoke_test_error.txt in CWD)
    # but succeed for fallback paths (containing .autosorter)
    original_open = open
    mock_app_dir = Path("/mock/home/.autosorter")

    def restricted_open(file, mode="r", *args, **kwargs):
        filepath = str(file)
        if "smoke_test_error.txt" in filepath and ".autosorter" not in filepath:
            raise PermissionError("Access denied to primary path")
        # For the mock home configuration path, return a mock file handle
        if ".autosorter" in filepath:
            m = MagicMock()
            m.__enter__.return_value = m
            return m
        return original_open(file, mode, *args, **kwargs)

    logger = logging.getLogger("app.main")
    with (
        patch("builtins.open", side_effect=restricted_open),
        patch("app.config.get_app_dir", return_value=mock_app_dir),
        patch.object(logger, "warning") as mock_warning,
    ):
        write_smoke_test_error("An error occurred", include_traceback=False)

        # Check that warning logged the fallback location being written successfully
        warning_calls = [c[0][0] for c in mock_warning.call_args_list]
        assert any(
            "Diagnostic log fallback write succeeded" in call_msg
            for call_msg in warning_calls
        )
        assert any(".autosorter" in call_msg for call_msg in warning_calls)


def test_write_smoke_test_error_all_blocked_handles_gracefully():
    """Verify that if all paths are blocked, fallback logging does not raise unhandled errors or crash startup."""

    def restricted_open_all(file, mode="r", *args, **kwargs):
        raise PermissionError("All directories are write-restricted!")

    logger = logging.getLogger("app.main")
    with (
        patch("builtins.open", side_effect=restricted_open_all),
        patch("tempfile.mkstemp", side_effect=PermissionError("System temp write-restricted!")),
        patch("app.config.get_app_dir", return_value=Path("/mock/home/.autosorter")),
        patch.object(logger, "error") as mock_error,
    ):
        # This call should execute without raising an exception and log the ultimate failure
        try:
            write_smoke_test_error("An error occurred", include_traceback=False)
        except PermissionError:
            pytest.fail(
                "write_smoke_test_error raised PermissionError instead of handling it gracefully"
            )

        error_calls = [c[0][0] for c in mock_error.call_args_list]
        assert any(
            "All diagnostic log write options failed" in call_msg
            for call_msg in error_calls
        )


def test_write_smoke_test_error_falls_back_to_system_temp_mkstemp(tmp_path, monkeypatch):
    """Verify system temporary directory fallback uses mkstemp with strict 0o600 permissions when primary and home paths fail."""
    import os
    import stat

    # Force primary and user home directory writes to fail
    def restricted_open(file, mode="r", *args, **kwargs):
        filepath = str(file)
        if "smoke_test_error.txt" in filepath:
            raise PermissionError("Primary and home paths restricted")
        return open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", restricted_open)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    write_smoke_test_error("Fallback system temp error", include_traceback=False)

    # Find the fallback file created in tmp_path
    created_files = list(tmp_path.glob("smoke_test_error_*.txt"))
    assert len(created_files) == 1
    fallback_file = created_files[0]

    assert fallback_file.exists()
    content = fallback_file.read_text(encoding="utf-8")
    assert "Fallback system temp error" in content

    # Verify 0o600 permissions on POSIX systems
    if os.name == "posix":
        file_mode = stat.S_IMODE(fallback_file.stat().st_mode)
        assert file_mode == 0o600


def test_write_smoke_test_error_sys_temp_chmod_failure_handled_gracefully(tmp_path, monkeypatch):
    """Verify that if os.chmod fails on non-POSIX or restricted filesystems, fallback writing still completes."""
    def restricted_open(file, mode="r", *args, **kwargs):
        filepath = str(file)
        if "smoke_test_error.txt" in filepath:
            raise PermissionError("Primary and home paths restricted")
        return open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", restricted_open)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr("os.chmod", MagicMock(side_effect=OSError("Operation not supported")))

    write_smoke_test_error("Chmod failure test error", include_traceback=False)

    created_files = list(tmp_path.glob("smoke_test_error_*.txt"))
    assert len(created_files) == 1
    assert created_files[0].read_text(encoding="utf-8").strip() == "Chmod failure test error"


def test_null_writer_and_reader_stream_properties():
    """Verify that NullWriter and NullReader correctly implement stream interface properties and methods."""
    from unittest.mock import MagicMock

    from app.main import ANSIStrippingWriter, NullReader, NullWriter

    writer = NullWriter()
    assert writer.closed is False
    assert writer.encoding == "utf-8"
    assert writer.errors == "replace"
    assert writer.isatty() is False
    assert writer.readable() is False
    assert writer.writable() is True
    assert writer.seekable() is False
    assert writer.write("test") == 4
    assert writer.write("") == 0
    with pytest.raises(OSError):
        writer.fileno()

    reader = NullReader()
    assert reader.closed is False
    assert reader.encoding == "utf-8"
    assert reader.errors == "replace"
    assert reader.isatty() is False
    assert reader.readable() is True
    assert reader.writable() is False
    assert reader.seekable() is False
    assert reader.read() == ""
    assert reader.readline() == ""
    assert reader.readlines() == []
    with pytest.raises(OSError):
        reader.fileno()

    mock_stream = MagicMock()
    mock_stream.closed = False
    mock_stream.encoding = "utf-8"

    wrapper = ANSIStrippingWriter(mock_stream)
    assert wrapper.closed is False
    assert wrapper.encoding == "utf-8"

    wrapper.write("\x1b[31mRed Text\x1b[0m")
    mock_stream.write.assert_called_once_with("Red Text")
