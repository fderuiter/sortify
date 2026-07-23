from unittest.mock import MagicMock, patch

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

    res = MetadataPass.run(str(tmp_path), ["subdir"], settings, db, None, None)
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
        res = MetadataPass.run(str(tmp_path), ["file1.txt"], settings, db, None, None)
        # Should not be bypassed because none of the invalid records should map hash123
        assert res == []


def test_metadata_and_text_identical_routing(tmp_path):
    from types import SimpleNamespace

    from app.core.analyzer import IncrementalAnalyzer
    from app.core.db import Database
    from app.core.db_worker import DBWorker
    from app.core.extractor import build_corpus_generator

    # 1. Setup rules in settings
    settings = SimpleNamespace(
        KEYWORD_RULES={"invoice": "Accounting", "tax": "Finances"},
        LEARNED_RULES={"receipt": "Finances"},
        MAX_WORKERS=2,
    )

    # Create directories
    base_dir = tmp_path / "documents"
    base_dir.mkdir()

    # 2. Setup mock files
    file_invoice = base_dir / "invoice_2026.txt"
    file_invoice.write_text("random invoice file contents")

    file_tax = base_dir / "tax_declaration.txt"
    file_tax.write_text("tax files details")

    file_content_invoice = base_dir / "generic_letter.txt"
    file_content_invoice.write_text("Hello, please find the invoice details here.")

    file_unrelated = base_dir / "neutral_report.txt"
    file_unrelated.write_text("The sky is blue today and the weather is warm.")

    file_historical = base_dir / "already_verified.txt"
    file_historical.write_text("some random contents")

    files = [
        "invoice_2026.txt",
        "tax_declaration.txt",
        "generic_letter.txt",
        "neutral_report.txt",
        "already_verified.txt",
    ]

    # Let's create database worker and database instances for BOTH sessions
    worker_a = DBWorker()
    db_a = Database(tmp_path / "autosorter_a.db", worker_a)

    # Put historically verified assignment in DB A
    db_a.upsert_document(
        str(base_dir),
        "already_verified.txt",
        "historical_hash_123",
        "some random contents",
    )
    db_a.set_user_verified_target(str(base_dir), "historical_hash_123", "Archive")

    # Mock the hashing specifically to return the correct historical hash for the historical file
    from app.core.extractor import get_file_hash

    original_get_file_hash = get_file_hash

    def mock_get_file_hash(filepath):
        if "already_verified.txt" in filepath:
            return "historical_hash_123"
        return original_get_file_hash(filepath)

    with (
        patch("app.core.metadata.get_file_hash", side_effect=mock_get_file_hash),
        patch("app.core.extractor.get_file_hash", side_effect=mock_get_file_hash),
    ):
        # ---- SESSION A: TWO-PASS METADATA-FIRST PIPELINE ----
        # Pass 1: run metadata discovery pass
        bypassed_files = MetadataPass.run(
            str(base_dir),
            files,
            settings,
            db_a,
            None,
            None,
        )

        assert set(bypassed_files) == {
            "invoice_2026.txt",
            "tax_declaration.txt",
            "already_verified.txt",
        }

        # Extract remaining files
        items_to_sort_a = [f for f in files if f not in bypassed_files]
        assert set(items_to_sort_a) == {"generic_letter.txt", "neutral_report.txt"}

        generator_a = build_corpus_generator(
            str(base_dir),
            items_to_sort_a,
            None,
            max_workers=1,
            db=db_a,
            settings=settings,
        )

        analyzer_a = IncrementalAnalyzer(max_folders=5, stop_words=set(), db=db_a)

        for chunk in generator_a:
            analyzer_a.partial_fit(str(base_dir), chunk, settings)

        plan_a = analyzer_a.generate_sorting_plan(str(base_dir), settings)

        # ---- SESSION B: TRADITIONAL EXTRACTION-FIRST PIPELINE ----
        worker_b = DBWorker()
        db_b = Database(tmp_path / "autosorter_b.db", worker_b)

        # Put historically verified assignment in DB B
        db_b.upsert_document(
            str(base_dir),
            "already_verified.txt",
            "historical_hash_123",
            "some random contents",
        )
        db_b.set_user_verified_target(str(base_dir), "historical_hash_123", "Archive")

        generator_b = build_corpus_generator(
            str(base_dir),
            files,
            None,
            max_workers=1,
            db=db_b,
            settings=settings,
        )

        analyzer_b = IncrementalAnalyzer(max_folders=5, stop_words=set(), db=db_b)

        for chunk in generator_b:
            analyzer_b.partial_fit(str(base_dir), chunk, settings)

        plan_b = analyzer_b.generate_sorting_plan(str(base_dir), settings)

        # Cleanup workers
        worker_a.stop()
        worker_b.stop()

        # 4. Compare resulting plans!
        def extract_routed_paths(plan_node, current_path=""):
            routes = {}
            for k, v in plan_node.items():
                if isinstance(v, dict) and v.get("__type__") == "file":
                    routes[k] = current_path
                elif isinstance(v, dict):
                    subpath = f"{current_path}/{k}" if current_path else k
                    routes.update(extract_routed_paths(v, subpath))
            return routes

        routes_a = extract_routed_paths(plan_a)
        routes_b = extract_routed_paths(plan_b)

        # Assert identical routing destinations for every single file
        assert routes_a == routes_b

        # Verify exact destinations
        assert routes_a["invoice_2026.txt"] == "Accounting"
        assert routes_a["tax_declaration.txt"] == "Finances"
        assert routes_a["generic_letter.txt"] == "Accounting"
        assert routes_a["already_verified.txt"] == "Archive"
