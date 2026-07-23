import logging
import os
from unittest.mock import MagicMock, patch

import pytest

from app.core.metadata import MetadataPass


def test_metadata_pass_empty_base_dir():
    assert MetadataPass.run(None, ["file1.txt"], None, None, None, None) == []
    assert MetadataPass.run("", ["file1.txt"], None, None, None, None) == []


def test_metadata_pass_cancel_check():
    settings = MagicMock()
    settings.KEYWORD_RULES = {}
    settings.LEARNED_RULES = {}
    db = MagicMock()
    db.get_all_documents.return_value = []

    cancel_check = MagicMock(return_value=True)

    # Even with items, cancel_check should break the loop instantly
    res = MetadataPass.run("/some/dir", ["file1.txt"], settings, db, None, cancel_check)
    assert res == []
    cancel_check.assert_called_once()


def test_metadata_pass_not_a_file(tmp_path):
    settings = MagicMock()
    settings.KEYWORD_RULES = {}
    settings.LEARNED_RULES = {}
    db = MagicMock()
    db.get_all_documents.return_value = []

    # "subdir" is a directory, not a file
    subdir = tmp_path / "subdir"
    subdir.mkdir()

    res = MetadataPass.run(
        str(tmp_path), ["subdir"], settings, db, None, None
    )
    assert res == []


def test_metadata_pass_access_error(tmp_path):
    settings = MagicMock()
    settings.KEYWORD_RULES = {}
    settings.LEARNED_RULES = {}
    db = MagicMock()
    db.get_all_documents.return_value = []

    file_path = tmp_path / "locked.txt"
    file_path.touch()

    # We mock 'open' to raise PermissionError when trying to open the file
    def mock_open_err(file, mode="r", *args, **kwargs):
        if "locked.txt" in str(file):
            raise PermissionError("Permission denied")
        return open(file, mode, *args, **kwargs)

    with patch("builtins.open", side_effect=mock_open_err):
        with patch("logging.warning") as mock_warn:
            res = MetadataPass.run(
                str(tmp_path), ["locked.txt"], settings, db, None, None
            )
            assert res == []
            mock_warn.assert_called_once()
            assert "locked.txt" in mock_warn.call_args[0][0]


def test_metadata_pass_cache_bypass(tmp_path):
    settings = MagicMock()
    settings.KEYWORD_RULES = {}
    settings.LEARNED_RULES = {}
    db = MagicMock()
    # d format: (filepath, decrypted_text, file_hash, user_verified_target_path)
    db.get_all_documents.return_value = [
        ("file1.txt", "some text", "hash123", "/dest/folder")
    ]

    file1 = tmp_path / "file1.txt"
    file1.touch()

    callback = MagicMock()

    with patch("app.core.metadata.get_file_hash", return_value="hash123"):
        res = MetadataPass.run(
            str(tmp_path), ["file1.txt"], settings, db, callback, None
        )
        assert res == ["file1.txt"]
        callback.assert_called_once()
        db.upsert_documents.assert_called_once_with(
            [(str(tmp_path), "file1.txt", "hash123", "[STATUS:BYPASSED]")]
        )


def test_metadata_pass_keyword_rule(tmp_path):
    settings = MagicMock()
    settings.KEYWORD_RULES = {"invoice": "/dest/invoices"}
    settings.LEARNED_RULES = {}
    db = MagicMock()
    db.get_all_documents.return_value = []

    file1 = tmp_path / "my_invoice_2026.pdf"
    file1.touch()

    with patch("app.core.metadata.get_file_hash", return_value="hash456"):
        res = MetadataPass.run(
            str(tmp_path), ["my_invoice_2026.pdf"], settings, db, None, None
        )
        assert res == ["my_invoice_2026.pdf"]
        db.upsert_documents.assert_called_once_with(
            [(str(tmp_path), "my_invoice_2026.pdf", "hash456", "[STATUS:BYPASSED]")]
        )


def test_metadata_pass_keyword_rule_with_empty_keyword(tmp_path):
    settings = MagicMock()
    settings.KEYWORD_RULES = {" ": "/dest/somewhere", "invoice": "/dest/invoices"}
    settings.LEARNED_RULES = {}
    db = MagicMock()
    db.get_all_documents.return_value = []

    file1 = tmp_path / "my_invoice_2026.pdf"
    file1.touch()

    with patch("app.core.metadata.get_file_hash", return_value="hash456"):
        res = MetadataPass.run(
            str(tmp_path), ["my_invoice_2026.pdf"], settings, db, None, None
        )
        assert res == ["my_invoice_2026.pdf"]


def test_metadata_pass_learned_rule(tmp_path):
    settings = MagicMock()
    settings.KEYWORD_RULES = {}
    settings.LEARNED_RULES = {"receipt": "/dest/receipts"}
    db = MagicMock()
    db.get_all_documents.return_value = []

    file1 = tmp_path / "super_receipt.png"
    file1.touch()

    with patch("app.core.metadata.get_file_hash", return_value="hash789"):
        res = MetadataPass.run(
            str(tmp_path), ["super_receipt.png"], settings, db, None, None
        )
        assert res == ["super_receipt.png"]
        db.upsert_documents.assert_called_once_with(
            [(str(tmp_path), "super_receipt.png", "hash789", "[STATUS:BYPASSED]")]
        )


def test_metadata_pass_learned_rule_with_empty_keyword(tmp_path):
    settings = MagicMock()
    settings.KEYWORD_RULES = {}
    settings.LEARNED_RULES = {"   ": "/dest/somewhere", "receipt": "/dest/receipts"}
    db = MagicMock()
    db.get_all_documents.return_value = []

    file1 = tmp_path / "super_receipt.png"
    file1.touch()

    with patch("app.core.metadata.get_file_hash", return_value="hash789"):
        res = MetadataPass.run(
            str(tmp_path), ["super_receipt.png"], settings, db, None, None
        )
        assert res == ["super_receipt.png"]


def test_metadata_pass_no_match(tmp_path):
    settings = MagicMock()
    settings.KEYWORD_RULES = {"invoice": "/dest/invoices"}
    settings.LEARNED_RULES = {"receipt": "/dest/receipts"}
    db = MagicMock()
    db.get_all_documents.return_value = []

    file1 = tmp_path / "unrelated.txt"
    file1.touch()

    with patch("app.core.metadata.get_file_hash", return_value="hashabc"):
        res = MetadataPass.run(
            str(tmp_path), ["unrelated.txt"], settings, db, None, None
        )
        assert res == []
        db.upsert_documents.assert_not_called()


def test_metadata_pass_no_settings_attributes(tmp_path):
    settings = object()  # Has no KEYWORD_RULES or LEARNED_RULES attributes
    db = MagicMock()
    db.get_all_documents.return_value = []

    file1 = tmp_path / "unrelated.txt"
    file1.touch()

    with patch("app.core.metadata.get_file_hash", return_value="hashabc"):
        res = MetadataPass.run(
            str(tmp_path), ["unrelated.txt"], settings, db, None, None
        )
        assert res == []


def test_metadata_pass_invalid_db_records(tmp_path):
    settings = MagicMock()
    settings.KEYWORD_RULES = {}
    settings.LEARNED_RULES = {}
    db = MagicMock()
    # Tuples of different invalid formats to test resilience/conditions
    db.get_all_documents.return_value = [
        ("file1.txt", "text"),  # len < 4
        ("file1.txt", "text", "", "/dest"),  # missing file_hash
        ("file1.txt", "text", "hash123", None),  # missing target path
    ]

    file1 = tmp_path / "file1.txt"
    file1.touch()

    with patch("app.core.metadata.get_file_hash", return_value="hash123"):
        res = MetadataPass.run(
            str(tmp_path), ["file1.txt"], settings, db, None, None
        )
        # Should not be bypassed because none of the invalid records should map hash123
        assert res == []
