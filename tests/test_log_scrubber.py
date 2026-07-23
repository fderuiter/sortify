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
