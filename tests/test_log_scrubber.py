import io
import logging
from pathlib import Path

import pytest

from app.log_filter import LogScrubbingFilter


@pytest.fixture
def memory_log():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("test_scrub")
    logger.setLevel(logging.DEBUG)
    # Clear existing handlers
    logger.handlers = []
    logger.addHandler(handler)

    # Add filter to handler
    home_dir = str(Path.home())
    filter_instance = LogScrubbingFilter(home_dir)
    handler.addFilter(filter_instance)

    return logger, stream


def test_scrub_message(memory_log):
    logger, stream = memory_log
    home = Path.home()
    logger.error(f"Error accessing {home}/secret/file.txt")
    assert "<USER_HOME>/secret/file.txt" in stream.getvalue()
    assert str(home) not in stream.getvalue()


def test_scrub_args(memory_log):
    logger, stream = memory_log
    home = Path.home()
    logger.error("Error with file %s", f"{home}/other/doc.pdf")
    assert "<USER_HOME>/other/doc.pdf" in stream.getvalue()
    assert str(home) not in stream.getvalue()


def test_scrub_path_object_args(memory_log):
    logger, stream = memory_log
    home = Path.home()
    file_path = home / "downloads" / "test.zip"
    logger.error("Failed path %s", file_path)
    output = stream.getvalue()
    assert "<USER_HOME>" in output
    assert "test.zip" in output
    assert str(home) not in output


def test_scrub_exception(memory_log):
    logger, stream = memory_log
    home = Path.home()
    try:
        raise ValueError(f"Bad path {home}/some/error")
    except ValueError:
        logger.error("An exception occurred", exc_info=True)

    output = stream.getvalue()
    assert "An exception occurred" in output
    assert "<USER_HOME>/some/error" in output
    assert str(home) not in output


def test_retain_relative_paths(memory_log):
    logger, stream = memory_log
    logger.error("Missing relative file ./docs/manual.pdf")
    assert "./docs/manual.pdf" in stream.getvalue()


def test_filter_encrypted_credentials(memory_log):
    logger, stream = memory_log
    logger.error("Normal log message about starting the service")
    logger.error("Failed to connect with proxy: enc:U2VjcmV0Q3JlZGVudGlhbHM=")

    output = stream.getvalue()
    assert "Normal log message about starting the service" in output
    assert "Failed to connect with proxy" not in output
    assert "enc:U2VjcmV0Q3JlZGVudGlhbHM=" not in output


def test_scrub_diagnostic_text_user_home_paths():
    from app.log_filter import scrub_diagnostic_text

    home = str(Path.home())
    text = f"Error occurred at {home}/config/app.json and {home.replace('/', '\\')}\\data\\db.sqlite"
    scrubbed = scrub_diagnostic_text(text)
    assert home not in scrubbed
    assert "<USER_HOME>/config/app.json" in scrubbed
    assert "<USER_HOME>\\data\\db.sqlite" in scrubbed


def test_scrub_diagnostic_text_sensitive_credentials():
    from app.log_filter import scrub_diagnostic_text

    raw_text = "Connect error: proxy=enc:U2VjcmV0VG9rZW4xMjM= password=super_secret Bearer eyJhbGciOiJIUzI1NiI="
    scrubbed = scrub_diagnostic_text(raw_text)

    assert "enc:" not in scrubbed
    assert "U2VjcmV0VG9rZW4xMjM=" not in scrubbed
    assert "super_secret" not in scrubbed
    assert "password=[REDACTED]" in scrubbed


def test_write_smoke_test_error_scrubs_diagnostic_file(tmp_path, monkeypatch):
    from app.main import write_smoke_test_error

    home = str(Path.home())
    diag_file = tmp_path / "smoke_test_error.txt"

    monkeypatch.chdir(tmp_path)
    err_msg = f"Startup failure at {home}/secrets with credential enc:MySecretTokenPass"

    try:
        raise ValueError(f"Traceback error in {home}/app/main.py")
    except ValueError:
        write_smoke_test_error(err_msg, include_traceback=True)

    assert diag_file.exists()
    content = diag_file.read_text(encoding="utf-8")

    assert home not in content
    assert "enc:" not in content
    assert "MySecretTokenPass" not in content
    assert "<USER_HOME>/secrets" in content
    assert "<USER_HOME>/app/main.py" in content


def test_write_smoke_test_error_fallback_scrubs_diagnostic_file(tmp_path, monkeypatch):
    from app.main import write_smoke_test_error

    home = Path.home()
    fallback_dir = tmp_path / "fallback_config"
    fallback_dir.mkdir()
    fallback_file = fallback_dir / "smoke_test_error.txt"

    original_open = open

    def restricted_open(file, mode="r", *args, **kwargs):
        filepath = str(file)
        if "smoke_test_error.txt" in filepath and str(fallback_dir) not in filepath:
            raise PermissionError("Access denied to primary location")
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", restricted_open)
    monkeypatch.setattr("app.config.get_app_dir", lambda: fallback_dir)

    err_msg = f"Failed with {home}/user_data and enc:SecretFallbackKey123"
    write_smoke_test_error(err_msg, include_traceback=False)

    assert fallback_file.exists()
    content = fallback_file.read_text(encoding="utf-8")

    assert str(home) not in content
    assert "enc:" not in content
    assert "SecretFallbackKey123" not in content
    assert "<USER_HOME>/user_data" in content

